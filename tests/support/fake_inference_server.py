"""Loopback HTTP server implementing the Inference v1 job contract."""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import urlparse

from video_preprocess.domain import (
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
)


Responder = Callable[[InferenceRequest], InferenceResponse]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _request_fingerprint(request: InferenceRequest) -> str:
    payload = {
        "task": request.task.value,
        "model": request.model.to_dict(),
        "inputs": request.to_dict()["inputs"],
        "parameters": dict(request.parameters),
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _default_responder(request: InferenceRequest) -> InferenceResponse:
    texts = request.inputs.get("texts")
    count = len(texts) if isinstance(texts, list) else 1
    vectors = [
        [1.0, 0.0] if index % 2 == 0 else [0.0, 1.0]
        for index in range(count)
    ]
    return InferenceResponse(
        request_id=request.request_id,
        status=InferenceStatus.SUCCEEDED,
        outputs={"vectors": vectors, "dimension": 2},
        model=EffectiveModel(
            provider="http.embedding",
            name=request.model.name,
            revision="fake-commit-1",
            runtime="fake-inference/1.0",
        ),
        usage={"input_count": count, "batch_size": count},
        timing={"queue_sec": 0.0, "inference_sec": 0.0},
    )


@dataclass(slots=True)
class _Job:
    request: InferenceRequest
    fingerprint: str
    status: str
    created_at: str
    updated_at: str
    response: InferenceResponse | None = None

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "schema_version": "1",
            "request_id": self.request.request_id,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.response is None:
            data["retry_after_sec"] = 0.01
        else:
            data["response"] = self.response.to_dict()
        return data


class FakeInferenceService:
    """Thread-safe in-memory job service behind the loopback server."""

    def __init__(
        self,
        *,
        auth_token: str | None = None,
        responder: Responder = _default_responder,
    ) -> None:
        self.auth_token = auth_token
        self.responder = responder
        self.capability = ProviderCapabilities(
            provider="fake.http.embedding",
            tasks=(InferenceTask.TEXT_EMBEDDING,),
            model_aliases=("embedding.remote",),
            input_media_types=("text/plain",),
            features=("normalized_vectors", "inline_batch"),
            max_batch_size=128,
            supports_cancellation=True,
            supports_async_jobs=True,
        )
        self.health = ProviderHealth(
            provider="fake.http.embedding",
            status=HealthState.AVAILABLE,
            details={"ready": True},
        )
        self.jobs: dict[str, _Job] = {}
        self.idempotency: dict[str, str] = {}
        self.inference_count = 0
        self._lock = threading.Lock()

    def authorize(self, value: str | None) -> bool:
        if self.auth_token is None:
            return True
        expected = f"Bearer {self.auth_token}"
        return value is not None and hmac.compare_digest(value, expected)

    def submit(
        self,
        request: InferenceRequest,
    ) -> tuple[int, dict[str, object]]:
        fingerprint = _request_fingerprint(request)
        with self._lock:
            existing_id = self.idempotency.get(request.idempotency_key)
            if existing_id is not None:
                existing = self.jobs[existing_id]
                if existing.fingerprint != fingerprint:
                    return HTTPStatus.CONFLICT, self.failure(
                        InferenceErrorCode.INVALID_REQUEST,
                        "idempotency key was reused with different input",
                        request_id=request.request_id,
                        details={"reason": "IDEMPOTENCY_KEY_CONFLICT"},
                    )
                return HTTPStatus.OK, existing.to_dict()
            if request.request_id in self.jobs:
                return HTTPStatus.CONFLICT, self.failure(
                    InferenceErrorCode.INVALID_REQUEST,
                    "request_id was reused with a different idempotency key",
                    request_id=request.request_id,
                    details={"reason": "REQUEST_ID_CONFLICT"},
                )
            now = _utc_now()
            job = _Job(
                request=request,
                fingerprint=fingerprint,
                status="queued",
                created_at=now,
                updated_at=now,
            )
            self.jobs[request.request_id] = job
            self.idempotency[request.idempotency_key] = request.request_id
            return HTTPStatus.ACCEPTED, job.to_dict()

    def poll(self, request_id: str) -> tuple[int, dict[str, object]]:
        with self._lock:
            job = self.jobs.get(request_id)
            if job is None:
                return HTTPStatus.NOT_FOUND, self.failure(
                    InferenceErrorCode.INVALID_REQUEST,
                    "inference job was not found",
                    request_id=request_id,
                    details={"reason": "JOB_NOT_FOUND"},
                )
            if job.status == "queued":
                job.status = "running"
                job.updated_at = _utc_now()
                return HTTPStatus.OK, job.to_dict()
            if job.status == "running":
                try:
                    response = self.responder(job.request)
                    if not isinstance(response, InferenceResponse):
                        raise TypeError("responder returned an invalid response")
                    if response.request_id != request_id:
                        raise ValueError("response request_id mismatch")
                except Exception as exc:
                    response = InferenceResponse(
                        request_id=request_id,
                        status=InferenceStatus.FAILED,
                        error=InferenceFailure(
                            code=InferenceErrorCode.INFERENCE_FAILED,
                            message="fake model execution failed",
                            retryable=False,
                            details={"error_type": type(exc).__name__},
                            request_id=request_id,
                        ),
                    )
                self.inference_count += 1
                job.response = response
                job.status = response.status.value
                job.updated_at = _utc_now()
            return HTTPStatus.OK, job.to_dict()

    def cancel(self, request_id: str) -> tuple[int, dict[str, object]]:
        with self._lock:
            job = self.jobs.get(request_id)
            if job is None:
                return HTTPStatus.NOT_FOUND, self.failure(
                    InferenceErrorCode.INVALID_REQUEST,
                    "inference job was not found",
                    request_id=request_id,
                    details={"reason": "JOB_NOT_FOUND"},
                )
            if job.response is not None:
                return HTTPStatus.OK, job.to_dict()
            response = InferenceResponse(
                request_id=request_id,
                status=InferenceStatus.CANCELLED,
                error=InferenceFailure(
                    code=InferenceErrorCode.CANCELLED,
                    message="inference job was cancelled",
                    retryable=False,
                    request_id=request_id,
                ),
            )
            job.status = "cancelled"
            job.response = response
            job.updated_at = _utc_now()
            return HTTPStatus.ACCEPTED, job.to_dict()

    @staticmethod
    def failure(
        code: InferenceErrorCode,
        message: str,
        *,
        request_id: str | None = None,
        details: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return InferenceFailure(
            code=code,
            message=message,
            retryable=False,
            details={} if details is None else details,
            request_id=request_id,
        ).to_dict()


class FakeInferenceServer:
    """Context-managed loopback HTTP server for provider contract tests."""

    def __init__(
        self,
        *,
        auth_token: str | None = None,
        responder: Responder = _default_responder,
    ) -> None:
        self.service = FakeInferenceService(
            auth_token=auth_token,
            responder=responder,
        )
        handler = self._handler_type(self.service)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="fake-inference-server",
            daemon=True,
        )

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def __enter__(self) -> "FakeInferenceServer":
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)

    @staticmethod
    def _handler_type(service: FakeInferenceService):
        class Handler(BaseHTTPRequestHandler):
            server_version = "FakeInference/1"

            def do_GET(self) -> None:
                path = urlparse(self.path).path
                if path == "/v1/health":
                    self._json(HTTPStatus.OK, service.health.to_dict())
                    return
                if not self._authorized():
                    return
                if path == "/v1/capabilities":
                    self._json(HTTPStatus.OK, service.capability.to_dict())
                    return
                request_id = self._request_id(path)
                if request_id is not None:
                    status, body = service.poll(request_id)
                    headers = (
                        {"Retry-After": "0"}
                        if body.get("status") in {"queued", "running"}
                        else None
                    )
                    self._json(status, body, headers=headers)
                    return
                self._not_found()

            def do_POST(self) -> None:
                path = urlparse(self.path).path
                if path != "/v1/inference-jobs":
                    self._not_found()
                    return
                if not self._authorized():
                    return
                try:
                    payload = self._request_json()
                    request = InferenceRequest.from_dict(payload)
                except Exception as exc:
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        service.failure(
                            InferenceErrorCode.INVALID_REQUEST,
                            "request body is not a valid InferenceRequest",
                            details={"error_type": type(exc).__name__},
                        ),
                    )
                    return
                header_key = self.headers.get("Idempotency-Key")
                if header_key != request.idempotency_key:
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        service.failure(
                            InferenceErrorCode.INVALID_REQUEST,
                            "Idempotency-Key must match request body",
                            request_id=request.request_id,
                            details={"reason": "IDEMPOTENCY_KEY_MISMATCH"},
                        ),
                    )
                    return
                status, body = service.submit(request)
                headers = (
                    {
                        "Location": (
                            f"/v1/inference-jobs/{body['request_id']}"
                        )
                    }
                    if status == HTTPStatus.ACCEPTED
                    else None
                )
                self._json(status, body, headers=headers)

            def do_DELETE(self) -> None:
                if not self._authorized():
                    return
                request_id = self._request_id(urlparse(self.path).path)
                if request_id is None:
                    self._not_found()
                    return
                status, body = service.cancel(request_id)
                self._json(status, body)

            def log_message(self, format, *args) -> None:
                return

            def _authorized(self) -> bool:
                if service.authorize(self.headers.get("Authorization")):
                    return True
                self._json(
                    HTTPStatus.UNAUTHORIZED,
                    service.failure(
                        InferenceErrorCode.AUTHENTICATION_FAILED,
                        "authentication failed",
                    ),
                )
                return False

            def _request_json(self) -> dict[str, object]:
                raw_length = self.headers.get("Content-Length")
                if raw_length is None:
                    raise ValueError("Content-Length is required")
                length = int(raw_length)
                if length < 1 or length > 1024 * 1024:
                    raise ValueError("request body size is invalid")
                value = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(value, dict):
                    raise TypeError("request body must be an object")
                return value

            @staticmethod
            def _request_id(path: str) -> str | None:
                prefix = "/v1/inference-jobs/"
                request_id = path[len(prefix):] if path.startswith(prefix) else ""
                if not request_id or "/" in request_id:
                    return None
                return request_id

            def _not_found(self) -> None:
                self._json(
                    HTTPStatus.NOT_FOUND,
                    service.failure(
                        InferenceErrorCode.INVALID_REQUEST,
                        "route was not found",
                        details={"reason": "ROUTE_NOT_FOUND"},
                    ),
                )

            def _json(
                self,
                status: int,
                body: dict[str, object],
                *,
                headers: dict[str, str] | None = None,
            ) -> None:
                payload = json.dumps(
                    body,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                self.send_response(int(status))
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(payload)

        return Handler
