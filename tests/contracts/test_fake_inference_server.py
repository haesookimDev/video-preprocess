"""HTTP-level contract tests for the local fake inference server."""

import http.client
import json
from dataclasses import replace
from urllib.parse import urlparse

import pytest

from video_preprocess.domain import (
    InferenceErrorCode,
    InferenceFailure,
    InferenceRequest,
    InferenceResponse,
    InferenceStatus,
    ProviderCapabilities,
    ProviderHealth,
    RequestedModel,
)

from tests.support.fake_inference_server import FakeInferenceServer


pytestmark = pytest.mark.integration


def make_request(**changes) -> InferenceRequest:
    request = InferenceRequest(
        request_id="infer_123",
        idempotency_key="idem_123",
        run_id="run_123",
        stage_run_id="stage_123",
        task="text_embedding",
        model=RequestedModel(
            alias="embedding.remote",
            name="example/embedding",
            revision="main",
        ),
        inputs={"texts": ["첫 번째", "두 번째"]},
        parameters={"normalize_embeddings": True},
        timeout_sec=30,
        trace_id="trace_123",
    )
    return replace(request, **changes)


def request_json(
    base_url: str,
    method: str,
    path: str,
    *,
    body: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], dict[str, object]]:
    parsed = urlparse(base_url)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
    payload = None
    request_headers = dict(headers or {})
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    connection.request(method, path, body=payload, headers=request_headers)
    response = connection.getresponse()
    response_body = json.loads(response.read().decode("utf-8"))
    response_headers = {name: value for name, value in response.getheaders()}
    connection.close()
    return response.status, response_headers, response_body


def submit(
    server: FakeInferenceServer,
    request: InferenceRequest,
    *,
    token: str | None = None,
) -> tuple[int, dict[str, str], dict[str, object]]:
    headers = {"Idempotency-Key": request.idempotency_key}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return request_json(
        server.base_url,
        "POST",
        "/v1/inference-jobs",
        body=request.to_dict(),
        headers=headers,
    )


def test_health_and_capabilities_use_domain_contracts() -> None:
    with FakeInferenceServer() as server:
        health_status, _, health_body = request_json(
            server.base_url, "GET", "/v1/health"
        )
        capability_status, _, capability_body = request_json(
            server.base_url, "GET", "/v1/capabilities"
        )

    assert health_status == 200
    assert ProviderHealth.from_dict(health_body).provider == "fake.http.embedding"
    assert capability_status == 200
    capabilities = ProviderCapabilities.from_dict(capability_body)
    assert capabilities.supports_async_jobs
    assert capabilities.supports_cancellation
    assert capabilities.effective_models["embedding.remote"].revision == (
        "fake-commit-1"
    )


def test_submit_poll_and_terminal_response_flow() -> None:
    with FakeInferenceServer() as server:
        status, headers, accepted = submit(server, make_request())
        first_poll = request_json(
            server.base_url, "GET", headers["Location"]
        )
        second_poll = request_json(
            server.base_url, "GET", headers["Location"]
        )

    assert status == 202
    assert accepted["status"] == "queued"
    assert first_poll[2]["status"] == "running"
    assert "Retry-After" in first_poll[1]
    assert second_poll[2]["status"] == "succeeded"
    response = InferenceResponse.from_dict(second_poll[2]["response"])
    assert response.status is InferenceStatus.SUCCEEDED
    assert response.model is not None
    assert response.model.provider == "http.embedding"
    assert response.model.revision == "fake-commit-1"


def test_matching_idempotent_submit_recovers_one_job() -> None:
    duplicate = replace(make_request(), request_id="infer_retry")
    with FakeInferenceServer() as server:
        first = submit(server, make_request())
        second = submit(server, duplicate)

    assert first[0] == 202
    assert second[0] == 200
    assert second[2]["request_id"] == "infer_123"
    assert len(server.service.jobs) == 1


def test_idempotency_conflict_is_structured_and_not_retryable() -> None:
    conflicting = replace(make_request(), inputs={"texts": ["변경됨"]})
    with FakeInferenceServer() as server:
        submit(server, make_request())
        status, _, body = submit(server, conflicting)

    failure = InferenceFailure.from_dict(body)
    assert status == 409
    assert failure.code is InferenceErrorCode.INVALID_REQUEST
    assert not failure.retryable
    assert failure.details["reason"] == "IDEMPOTENCY_KEY_CONFLICT"


def test_authentication_failure_never_echoes_token() -> None:
    secret = "test-secret-token"
    with FakeInferenceServer(auth_token=secret) as server:
        status, _, body = submit(server, make_request(), token="wrong")
        ok_status, _, _ = submit(server, make_request(), token=secret)

    failure = InferenceFailure.from_dict(body)
    assert status == 401
    assert failure.code is InferenceErrorCode.AUTHENTICATION_FAILED
    assert secret not in json.dumps(body)
    assert ok_status == 202


def test_cancel_is_cooperative_terminal_and_idempotent() -> None:
    with FakeInferenceServer() as server:
        _, headers, _ = submit(server, make_request())
        first = request_json(
            server.base_url, "DELETE", headers["Location"]
        )
        second = request_json(
            server.base_url, "DELETE", headers["Location"]
        )
        polled = request_json(
            server.base_url, "GET", headers["Location"]
        )

    assert first[0] == 202
    assert second[0] == 200
    assert polled[2]["status"] == "cancelled"
    response = InferenceResponse.from_dict(polled[2]["response"])
    assert response.status is InferenceStatus.CANCELLED
    assert response.error is not None
    assert response.error.code is InferenceErrorCode.CANCELLED


def test_header_body_idempotency_mismatch_is_rejected() -> None:
    request = make_request()
    with FakeInferenceServer() as server:
        status, _, body = request_json(
            server.base_url,
            "POST",
            "/v1/inference-jobs",
            body=request.to_dict(),
            headers={"Idempotency-Key": "different"},
        )

    failure = InferenceFailure.from_dict(body)
    assert status == 400
    assert failure.details["reason"] == "IDEMPOTENCY_KEY_MISMATCH"
