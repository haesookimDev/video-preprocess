"""Network-free tests for the production Inference HTTP job service."""

import asyncio
import time
from dataclasses import replace

from video_preprocess.domain import (
    EffectiveModel,
    HealthState,
    InferenceErrorCode,
    InferenceRequest,
    InferenceResponse,
    InferenceStatus,
    InferenceTask,
    ProviderCapabilities,
    ProviderHealth,
    RequestedModel,
)
from video_preprocess.inference import InferenceHTTPService


def make_request(**changes) -> InferenceRequest:
    request = InferenceRequest(
        request_id="infer_123",
        idempotency_key="idem_123",
        run_id="run_123",
        stage_run_id="stage_123",
        task=InferenceTask.TEXT_EMBEDDING,
        model=RequestedModel(
            alias="embedding.default",
            name="example/embedding",
            revision="default",
        ),
        inputs={"texts": ["첫 번째"]},
        parameters={"normalize_embeddings": True},
        timeout_sec=2,
        trace_id="trace_123",
    )
    return replace(request, **changes)


class Provider:
    def __init__(self) -> None:
        self.infer_count = 0
        self.cancelled = []

    async def capabilities(self):
        return ProviderCapabilities(
            provider="test.embedding",
            tasks=(InferenceTask.TEXT_EMBEDDING,),
            model_aliases=("embedding.default",),
            max_batch_size=8,
            supports_cancellation=True,
        )

    async def effective_model(self):
        return EffectiveModel(
            provider="test.embedding",
            name="example/embedding",
            revision="commit-123",
            runtime="test/1",
        )

    async def health(self):
        return ProviderHealth(
            provider="test.embedding",
            status=HealthState.AVAILABLE,
        )

    async def infer(self, request):
        self.infer_count += 1
        return InferenceResponse(
            request_id=request.request_id,
            status=InferenceStatus.SUCCEEDED,
            outputs={"vectors": [[1.0, 0.0]], "dimension": 2},
            model=await self.effective_model(),
        )

    async def cancel(self, request_id):
        self.cancelled.append(request_id)


def poll_terminal(service, request_id="infer_123"):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        status, body = service.poll(request_id)
        if body.get("status") in {"succeeded", "failed", "cancelled"}:
            return status, body
        time.sleep(0.001)
    raise AssertionError("job did not become terminal")


def test_service_exposes_effective_capability_and_executes_job() -> None:
    provider = Provider()
    service = InferenceHTTPService(
        alias="embedding.default",
        provider=provider,
        retry_after_sec=0.001,
    )
    try:
        status, capabilities = service.capabilities()
        accepted_status, accepted = service.submit(
            make_request(),
            idempotency_key="idem_123",
        )
        terminal_status, terminal = poll_terminal(service)
    finally:
        service.close()

    assert status == 200
    assert capabilities["effective_models"]["embedding.default"][
        "revision"
    ] == "commit-123"
    assert accepted_status == 202
    assert accepted["status"] == "queued"
    assert terminal_status == 200
    assert terminal["status"] == "succeeded"
    assert provider.infer_count == 1


def test_service_recovers_idempotent_job_and_rejects_conflict() -> None:
    provider = Provider()
    service = InferenceHTTPService(
        alias="embedding.default",
        provider=provider,
    )
    try:
        first_status, _ = service.submit(
            make_request(),
            idempotency_key="idem_123",
        )
        recovered_status, recovered = service.submit(
            make_request(request_id="infer_retry"),
            idempotency_key="idem_123",
        )
        conflict_status, conflict = service.submit(
            make_request(
                request_id="infer_conflict",
                inputs={"texts": ["변경된 입력"]},
            ),
            idempotency_key="idem_123",
        )
        poll_terminal(service)
    finally:
        service.close()

    assert first_status == 202
    assert recovered_status == 200
    assert recovered["request_id"] == "infer_123"
    assert conflict_status == 409
    assert conflict["code"] == InferenceErrorCode.INVALID_REQUEST.value
    assert conflict["details"]["reason"] == "IDEMPOTENCY_KEY_CONFLICT"
    assert provider.infer_count == 1


def test_service_cancels_running_job_and_applies_capacity_limit() -> None:
    class BlockingProvider(Provider):
        def __init__(self) -> None:
            super().__init__()
            self.started = None

        async def infer(self, request):
            self.infer_count += 1
            self.started = asyncio.Event()
            self.started.set()
            await asyncio.Future()

    provider = BlockingProvider()
    service = InferenceHTTPService(
        alias="embedding.default",
        provider=provider,
        max_jobs=1,
    )
    try:
        accepted_status, _ = service.submit(
            make_request(),
            idempotency_key="idem_123",
        )
        deadline = time.monotonic() + 2
        while provider.started is None and time.monotonic() < deadline:
            time.sleep(0.001)
        limited_status, limited = service.submit(
            make_request(
                request_id="infer_456",
                idempotency_key="idem_456",
            ),
            idempotency_key="idem_456",
        )
        cancelled_status, cancelled = service.cancel("infer_123")
    finally:
        service.close()

    assert accepted_status == 202
    assert limited_status == 429
    assert limited["code"] == InferenceErrorCode.PROVIDER_RATE_LIMITED.value
    assert cancelled_status == 202
    assert cancelled["status"] == "cancelled"
    assert provider.cancelled == ["infer_123"]
