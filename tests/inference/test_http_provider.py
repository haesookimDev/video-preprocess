"""Network-free tests for the HTTP Inference v1 provider."""

import asyncio
import json
from dataclasses import replace

import pytest

from video_preprocess.domain import (
    ArtifactRef,
    Checksum,
    EffectiveModel,
    HealthState,
    InferenceErrorCode,
    InferenceFailure,
    InferenceRequest,
    InferenceResponse,
    InferenceStatus,
    InferenceTask,
    ProviderCapabilities,
    ProviderHealth,
    RequestedModel,
)
from video_preprocess.inference import HTTPInferenceProvider, HTTPRetryPolicy
from video_preprocess.inference.http import HTTPTransportResponse


def make_request(**changes) -> InferenceRequest:
    request = InferenceRequest(
        request_id="infer_123",
        idempotency_key="idem_123",
        run_id="run_123",
        stage_run_id="stage_123",
        task=InferenceTask.TEXT_EMBEDDING,
        model=RequestedModel(
            alias="embedding.remote",
            name="example/embedding",
            revision="main",
        ),
        inputs={"texts": ["첫 번째", "두 번째"]},
        parameters={"normalize_embeddings": True},
        timeout_sec=2,
        trace_id="trace_123",
    )
    return replace(request, **changes)


def capabilities_body() -> dict[str, object]:
    return ProviderCapabilities(
        provider="remote.embedding",
        tasks=(InferenceTask.TEXT_EMBEDDING,),
        model_aliases=("embedding.remote",),
        max_batch_size=128,
        supports_cancellation=True,
        supports_async_jobs=True,
        effective_models={
            "embedding.remote": EffectiveModel(
                provider="remote.embedding",
                name="example/embedding",
                revision="commit-123",
                runtime="remote/1",
            )
        },
    ).to_dict()


def success_response(request_id: str) -> InferenceResponse:
    return InferenceResponse(
        request_id=request_id,
        status=InferenceStatus.SUCCEEDED,
        outputs={"vectors": [[1.0, 0.0], [0.0, 1.0]], "dimension": 2},
        model=EffectiveModel(
            provider="remote.embedding",
            name="example/embedding",
            revision="commit-123",
            runtime="remote/1",
        ),
    )


def job_body(
    status: str,
    *,
    request_id: str = "infer_123",
) -> dict[str, object]:
    body: dict[str, object] = {
        "schema_version": "1",
        "request_id": request_id,
        "status": status,
        "created_at": "2026-08-12T00:00:00Z",
        "updated_at": "2026-08-12T00:00:00Z",
    }
    if status in {"queued", "running"}:
        body["retry_after_sec"] = 0
    else:
        body["response"] = success_response(request_id).to_dict()
    return body


def json_response(
    status: int,
    body: dict[str, object],
    *,
    headers: dict[str, str] | None = None,
) -> HTTPTransportResponse:
    return HTTPTransportResponse(
        status=status,
        headers={} if headers is None else headers,
        body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
    )


class ScriptedTransport:
    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    async def request(
        self,
        method,
        url,
        *,
        headers,
        body,
        timeout_sec,
    ) -> HTTPTransportResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout_sec": timeout_sec,
            }
        )
        outcome = self.responses.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def make_provider(transport, **changes) -> HTTPInferenceProvider:
    settings = {
        "alias": "embedding.remote",
        "endpoint": "https://models.example.test/base",
        "transport": transport,
        "poll_interval_sec": 0.001,
        "max_poll_interval_sec": 0.01,
        "retry_policy": HTTPRetryPolicy(
            max_attempts=3,
            initial_backoff_sec=0,
            max_backoff_sec=0,
            jitter_ratio=0,
            circuit_failure_threshold=10,
        ),
    }
    settings.update(changes)
    return HTTPInferenceProvider(**settings)


def test_capabilities_health_and_effective_model_use_cached_contract() -> None:
    health = ProviderHealth(
        provider="remote.embedding",
        status=HealthState.AVAILABLE,
    )
    transport = ScriptedTransport(
        json_response(200, capabilities_body()),
        json_response(200, health.to_dict()),
    )
    provider = make_provider(transport)

    async def exercise():
        first = await provider.capabilities()
        second = await provider.capabilities()
        model = await provider.effective_model()
        provider_health = await provider.health()
        return first, second, model, provider_health

    first, second, model, provider_health = asyncio.run(exercise())

    assert first is second
    assert model is not None
    assert model.revision == "commit-123"
    assert provider_health.status is HealthState.AVAILABLE
    assert [call["method"] for call in transport.calls] == ["GET", "GET"]


def test_submit_poll_flow_sends_auth_and_idempotency_headers() -> None:
    transport = ScriptedTransport(
        json_response(202, job_body("queued")),
        json_response(200, job_body("running")),
        json_response(200, job_body("succeeded")),
    )
    provider = make_provider(transport, auth_token="top-secret")

    response = asyncio.run(provider.infer(make_request()))

    assert response.status is InferenceStatus.SUCCEEDED
    assert response.outputs["dimension"] == 2
    assert [call["method"] for call in transport.calls] == [
        "POST",
        "GET",
        "GET",
    ]
    assert transport.calls[0]["headers"]["Idempotency-Key"] == "idem_123"
    assert transport.calls[0]["headers"]["Authorization"] == (
        "Bearer top-secret"
    )


def test_idempotent_recovery_polls_existing_remote_request_id() -> None:
    remote_id = "infer_original"
    transport = ScriptedTransport(
        json_response(200, job_body("queued", request_id=remote_id)),
        json_response(200, job_body("succeeded", request_id=remote_id)),
    )
    provider = make_provider(transport)

    response = asyncio.run(provider.infer(make_request()))

    assert response.request_id == "infer_123"
    assert response.status is InferenceStatus.SUCCEEDED
    assert transport.calls[1]["url"].endswith(
        "/v1/inference-jobs/infer_original"
    )


def test_retry_after_is_honored_for_rate_limit() -> None:
    sleeps = []

    async def record_sleep(delay):
        sleeps.append(delay)

    failure = InferenceFailure(
        code=InferenceErrorCode.PROVIDER_RATE_LIMITED,
        message="try later",
        retryable=True,
        request_id="infer_123",
    )
    transport = ScriptedTransport(
        json_response(429, failure.to_dict(), headers={"Retry-After": "0.25"}),
        json_response(200, job_body("succeeded")),
    )
    provider = make_provider(transport, sleep=record_sleep)

    response = asyncio.run(provider.infer(make_request()))

    assert response.status is InferenceStatus.SUCCEEDED
    assert sleeps == [0.25]
    assert len(transport.calls) == 2


def test_invalid_retryable_error_body_still_retries() -> None:
    transport = ScriptedTransport(
        HTTPTransportResponse(503, {}, b"upstream unavailable"),
        json_response(200, job_body("succeeded")),
    )
    provider = make_provider(transport)

    response = asyncio.run(provider.infer(make_request()))

    assert response.status is InferenceStatus.SUCCEEDED
    assert len(transport.calls) == 2


def test_auth_failure_is_normalized_without_leaking_token() -> None:
    failure = InferenceFailure(
        code=InferenceErrorCode.AUTHENTICATION_FAILED,
        message="authentication failed",
        retryable=False,
    )
    transport = ScriptedTransport(json_response(401, failure.to_dict()))
    provider = make_provider(transport, auth_token="never-print-this")

    response = asyncio.run(provider.infer(make_request()))

    assert response.error is not None
    assert response.error.code is InferenceErrorCode.AUTHENTICATION_FAILED
    assert response.error.request_id == "infer_123"
    assert "never-print-this" not in json.dumps(response.to_dict())
    assert len(transport.calls) == 1


def test_circuit_opens_after_repeated_upstream_failures() -> None:
    transport = ScriptedTransport(
        HTTPTransportResponse(503, {}, b"unavailable"),
        HTTPTransportResponse(503, {}, b"unavailable"),
    )
    policy = HTTPRetryPolicy(
        max_attempts=2,
        initial_backoff_sec=0,
        max_backoff_sec=0,
        jitter_ratio=0,
        circuit_failure_threshold=2,
    )
    provider = make_provider(transport, retry_policy=policy)

    first = asyncio.run(provider.infer(make_request()))
    second = asyncio.run(
        provider.infer(
            make_request(
                request_id="infer_456",
                idempotency_key="idem_456",
            )
        )
    )

    assert first.error is not None
    assert first.error.code is InferenceErrorCode.PROVIDER_UNAVAILABLE
    assert second.error is not None
    assert second.error.details["reason"] == "CIRCUIT_OPEN"
    assert len(transport.calls) == 2


def test_disallowed_artifact_namespace_fails_before_transport() -> None:
    artifact = ArtifactRef(
        artifact_id="audio_1",
        kind="audio",
        uri="artifact://run_123/audio.wav",
        media_type="audio/wav",
        size_bytes=100,
        checksum=Checksum("sha256", "abc"),
    )
    request = make_request(inputs={"audio": artifact})
    transport = ScriptedTransport()
    provider = make_provider(transport)

    response = asyncio.run(provider.infer(request))

    assert response.error is not None
    assert response.error.code is InferenceErrorCode.INVALID_REQUEST
    assert not transport.calls


def test_task_cancellation_dispatches_remote_delete() -> None:
    class BlockingTransport:
        def __init__(self) -> None:
            self.poll_started = asyncio.Event()
            self.methods = []

        async def request(
            self,
            method,
            url,
            *,
            headers,
            body,
            timeout_sec,
        ) -> HTTPTransportResponse:
            self.methods.append(method)
            if method == "POST":
                return json_response(202, job_body("queued"))
            if method == "GET":
                self.poll_started.set()
                await asyncio.Future()
            return json_response(202, {
                **job_body("queued"),
                "status": "cancelled",
                "response": InferenceResponse(
                    request_id="infer_123",
                    status=InferenceStatus.CANCELLED,
                    error=InferenceFailure(
                        code=InferenceErrorCode.CANCELLED,
                        message="cancelled",
                        retryable=False,
                        request_id="infer_123",
                    ),
                ).to_dict(),
            })

    async def exercise():
        transport = BlockingTransport()
        provider = make_provider(transport)
        task = asyncio.create_task(provider.infer(make_request()))
        await transport.poll_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return transport

    transport = asyncio.run(exercise())

    assert transport.methods == ["POST", "GET", "DELETE"]


def test_retry_policy_rejects_invalid_random_source_value() -> None:
    policy = HTTPRetryPolicy()

    with pytest.raises(ValueError, match="random_value"):
        policy.backoff_sec(attempts_used=1, random_value=2)
