"""Production HTTP Inference v1 adapter for an Inference Provider."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import threading
from concurrent.futures import Future
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Coroutine
from urllib.parse import urlparse

from video_preprocess.domain import (
    InferenceErrorCode,
    InferenceFailure,
    InferenceRequest,
    InferenceResponse,
    InferenceStatus,
    ProviderCapabilities,
)
from video_preprocess.inference.gateway import InferenceGateway
from video_preprocess.inference.provider import InferenceProvider


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


class _AsyncProviderRuntime:
    """Own one event loop shared by all HTTP handler threads."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._closed = False
        self._thread = threading.Thread(
            target=self._run,
            name="inference-provider-runtime",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise RuntimeError("inference provider runtime did not start")

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()
        pending = asyncio.all_tasks(self._loop)
        for task in pending:
            task.cancel()
        if pending:
            self._loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True)
            )
        self._loop.close()

    def submit(self, coroutine: Coroutine) -> Future:
        if self._closed:
            coroutine.close()
            raise RuntimeError("inference provider runtime is closed")
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    def call(self, coroutine: Coroutine, *, timeout_sec: float = 10.0):
        future = self.submit(coroutine)
        try:
            return future.result(timeout=timeout_sec)
        except Exception:
            future.cancel()
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)


@dataclass(slots=True)
class _InferenceJob:
    request: InferenceRequest
    fingerprint: str
    status: str
    created_at: str
    updated_at: str
    response: InferenceResponse | None = None
    future: Future | None = None

    @property
    def terminal(self) -> bool:
        return self.status in {"succeeded", "failed", "cancelled"}

    def to_dict(self, *, retry_after_sec: float) -> dict[str, object]:
        data: dict[str, object] = {
            "schema_version": "1",
            "request_id": self.request.request_id,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.response is None:
            data["retry_after_sec"] = retry_after_sec
        else:
            data["response"] = self.response.to_dict()
        return data


class InferenceHTTPService:
    """Thread-safe async-job application service behind the HTTP adapter."""

    def __init__(
        self,
        *,
        alias: str,
        provider: InferenceProvider,
        max_jobs: int = 1024,
        retry_after_sec: float = 0.1,
        provider_call_timeout_sec: float = 10.0,
    ) -> None:
        if not isinstance(alias, str) or not alias.strip():
            raise ValueError("alias must be a non-empty string")
        for method_name in ("capabilities", "health", "infer", "cancel"):
            if not callable(getattr(provider, method_name, None)):
                raise TypeError(
                    f"provider must implement {method_name}"
                )
        if isinstance(max_jobs, bool) or not isinstance(max_jobs, int):
            raise TypeError("max_jobs must be an integer")
        if max_jobs < 1:
            raise ValueError("max_jobs must be positive")
        for value, field_name in (
            (retry_after_sec, "retry_after_sec"),
            (provider_call_timeout_sec, "provider_call_timeout_sec"),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value <= 0
            ):
                raise ValueError(f"{field_name} must be positive")
        self.alias = alias.strip()
        self.provider = provider
        self.gateway = InferenceGateway({self.alias: provider})
        self.max_jobs = max_jobs
        self.retry_after_sec = float(retry_after_sec)
        self.provider_call_timeout_sec = float(provider_call_timeout_sec)
        self._jobs: dict[str, _InferenceJob] = {}
        self._idempotency: dict[str, str] = {}
        self._lock = threading.Lock()
        self._runtime = _AsyncProviderRuntime()

    def close(self) -> None:
        self._runtime.close()

    def health(self) -> tuple[int, dict[str, object]]:
        try:
            health = self._runtime.call(
                self.provider.health(),
                timeout_sec=self.provider_call_timeout_sec,
            )
            return HTTPStatus.OK, health.to_dict()
        except Exception as exc:
            return HTTPStatus.SERVICE_UNAVAILABLE, self._failure(
                InferenceErrorCode.PROVIDER_UNAVAILABLE,
                "provider health check failed",
                retryable=True,
                details={"error_type": type(exc).__name__},
            )

    def capabilities(self) -> tuple[int, dict[str, object]]:
        try:
            capabilities = self._runtime.call(
                self._capabilities(),
                timeout_sec=self.provider_call_timeout_sec,
            )
            return HTTPStatus.OK, capabilities.to_dict()
        except Exception as exc:
            return HTTPStatus.SERVICE_UNAVAILABLE, self._failure(
                InferenceErrorCode.PROVIDER_UNAVAILABLE,
                "provider capability check failed",
                retryable=True,
                details={"error_type": type(exc).__name__},
            )

    async def _capabilities(self) -> ProviderCapabilities:
        capabilities = await self.provider.capabilities()
        if not isinstance(capabilities, ProviderCapabilities):
            raise TypeError("provider returned invalid capabilities")
        if self.alias not in capabilities.model_aliases:
            raise ValueError("provider capabilities omit configured alias")
        effective_models = dict(capabilities.effective_models)
        resolver = getattr(self.provider, "effective_model", None)
        if callable(resolver):
            effective = await resolver()
            if effective is not None:
                effective_models[self.alias] = effective
        return replace(capabilities, effective_models=effective_models)

    def submit(
        self,
        request: InferenceRequest,
        *,
        idempotency_key: str | None,
    ) -> tuple[int, dict[str, object]]:
        if idempotency_key != request.idempotency_key:
            return HTTPStatus.BAD_REQUEST, self._failure(
                InferenceErrorCode.INVALID_REQUEST,
                "Idempotency-Key must match request body",
                retryable=False,
                request_id=request.request_id,
                details={"reason": "IDEMPOTENCY_KEY_MISMATCH"},
            )
        if request.model.alias != self.alias:
            return HTTPStatus.BAD_REQUEST, self._failure(
                InferenceErrorCode.UNSUPPORTED_CAPABILITY,
                "request alias does not match server binding",
                retryable=False,
                request_id=request.request_id,
            )
        fingerprint = _request_fingerprint(request)
        with self._lock:
            existing_id = self._idempotency.get(request.idempotency_key)
            if existing_id is not None:
                existing = self._jobs[existing_id]
                if existing.fingerprint != fingerprint:
                    return HTTPStatus.CONFLICT, self._failure(
                        InferenceErrorCode.INVALID_REQUEST,
                        "idempotency key was reused with different input",
                        retryable=False,
                        request_id=request.request_id,
                        details={"reason": "IDEMPOTENCY_KEY_CONFLICT"},
                    )
                return HTTPStatus.OK, existing.to_dict(
                    retry_after_sec=self.retry_after_sec
                )
            if request.request_id in self._jobs:
                return HTTPStatus.CONFLICT, self._failure(
                    InferenceErrorCode.INVALID_REQUEST,
                    "request_id was reused with a different idempotency key",
                    retryable=False,
                    request_id=request.request_id,
                    details={"reason": "REQUEST_ID_CONFLICT"},
                )
            self._prune_terminal_jobs()
            if len(self._jobs) >= self.max_jobs:
                return HTTPStatus.TOO_MANY_REQUESTS, self._failure(
                    InferenceErrorCode.PROVIDER_RATE_LIMITED,
                    "inference job capacity is exhausted",
                    retryable=True,
                    request_id=request.request_id,
                )
            now = _utc_now()
            job = _InferenceJob(
                request=request,
                fingerprint=fingerprint,
                status="queued",
                created_at=now,
                updated_at=now,
            )
            self._jobs[request.request_id] = job
            self._idempotency[request.idempotency_key] = request.request_id
            accepted = job.to_dict(retry_after_sec=self.retry_after_sec)
        future = self._runtime.submit(self._execute(request.request_id))
        with self._lock:
            job.future = future
        return HTTPStatus.ACCEPTED, accepted

    async def _execute(self, request_id: str) -> None:
        with self._lock:
            job = self._jobs.get(request_id)
            if job is None or job.terminal:
                return
            job.status = "running"
            job.updated_at = _utc_now()
            request = job.request
        try:
            response = await self.gateway.infer(request)
            if not isinstance(response, InferenceResponse):
                raise TypeError("gateway returned invalid response")
        except asyncio.CancelledError:
            self._mark_cancelled(request_id)
            return
        except Exception as exc:
            response = InferenceResponse(
                request_id=request_id,
                status=InferenceStatus.FAILED,
                error=InferenceFailure(
                    code=InferenceErrorCode.PROVIDER_UNAVAILABLE,
                    message="provider execution failed unexpectedly",
                    retryable=True,
                    details={"error_type": type(exc).__name__},
                    request_id=request_id,
                ),
            )
        with self._lock:
            job = self._jobs.get(request_id)
            if job is None or job.status == "cancelled":
                return
            job.response = response
            job.status = response.status.value
            job.updated_at = _utc_now()

    def poll(self, request_id: str) -> tuple[int, dict[str, object]]:
        with self._lock:
            job = self._jobs.get(request_id)
            if job is None:
                return HTTPStatus.NOT_FOUND, self._failure(
                    InferenceErrorCode.INVALID_REQUEST,
                    "inference job was not found",
                    retryable=False,
                    request_id=request_id,
                    details={"reason": "JOB_NOT_FOUND"},
                )
            return HTTPStatus.OK, job.to_dict(
                retry_after_sec=self.retry_after_sec
            )

    def cancel(self, request_id: str) -> tuple[int, dict[str, object]]:
        with self._lock:
            job = self._jobs.get(request_id)
            if job is None:
                return HTTPStatus.NOT_FOUND, self._failure(
                    InferenceErrorCode.INVALID_REQUEST,
                    "inference job was not found",
                    retryable=False,
                    request_id=request_id,
                    details={"reason": "JOB_NOT_FOUND"},
                )
            if job.terminal:
                return HTTPStatus.OK, job.to_dict(
                    retry_after_sec=self.retry_after_sec
                )
            future = job.future
        try:
            self._runtime.call(
                self.gateway.cancel(request_id),
                timeout_sec=self.provider_call_timeout_sec,
            )
        except Exception:
            pass
        if future is not None:
            future.cancel()
        self._mark_cancelled(request_id)
        with self._lock:
            return HTTPStatus.ACCEPTED, self._jobs[request_id].to_dict(
                retry_after_sec=self.retry_after_sec
            )

    def _mark_cancelled(self, request_id: str) -> None:
        with self._lock:
            job = self._jobs.get(request_id)
            if job is None or job.terminal:
                return
            job.status = "cancelled"
            job.updated_at = _utc_now()
            job.response = InferenceResponse(
                request_id=request_id,
                status=InferenceStatus.CANCELLED,
                error=InferenceFailure(
                    code=InferenceErrorCode.CANCELLED,
                    message="inference job was cancelled",
                    retryable=False,
                    request_id=request_id,
                ),
            )

    def _prune_terminal_jobs(self) -> None:
        while len(self._jobs) >= self.max_jobs:
            removable_id = next(
                (
                    request_id
                    for request_id, job in self._jobs.items()
                    if job.terminal
                ),
                None,
            )
            if removable_id is None:
                return
            removed = self._jobs.pop(removable_id)
            self._idempotency.pop(removed.request.idempotency_key, None)

    @staticmethod
    def _failure(
        code: InferenceErrorCode,
        message: str,
        *,
        retryable: bool,
        request_id: str | None = None,
        details: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return InferenceFailure(
            code=code,
            message=message,
            retryable=retryable,
            details={} if details is None else details,
            request_id=request_id,
        ).to_dict()


class InferenceHTTPServer:
    """Bind the production job service to a stdlib threaded HTTP server."""

    def __init__(
        self,
        *,
        alias: str,
        provider: InferenceProvider,
        host: str = "127.0.0.1",
        port: int = 8080,
        auth_token: str | None = None,
        max_request_bytes: int = 1024 * 1024,
        max_jobs: int = 1024,
    ) -> None:
        if auth_token is not None and (
            not isinstance(auth_token, str) or not auth_token.strip()
        ):
            raise ValueError("auth_token must be non-empty or None")
        if (
            isinstance(max_request_bytes, bool)
            or not isinstance(max_request_bytes, int)
            or max_request_bytes < 1
        ):
            raise ValueError("max_request_bytes must be a positive integer")
        self.auth_token = (
            None if auth_token is None else auth_token.strip()
        )
        self.max_request_bytes = max_request_bytes
        self.service = InferenceHTTPService(
            alias=alias,
            provider=provider,
            max_jobs=max_jobs,
        )
        try:
            self._server = ThreadingHTTPServer(
                (host, port),
                self._handler_type(),
            )
        except Exception:
            self.service.close()
            raise
        self._server.daemon_threads = True
        self._thread: threading.Thread | None = None
        self._closed = False

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def serve_forever(self) -> None:
        try:
            self._server.serve_forever()
        finally:
            self.close()

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("inference HTTP server is already started")
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="inference-http-server",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._thread is not None:
            self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.service.close()

    def __enter__(self) -> "InferenceHTTPServer":
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def _handler_type(self):
        adapter = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "VideoPreprocessInference/1"

            def do_GET(self) -> None:
                path = urlparse(self.path).path
                if path == "/v1/health":
                    self._send(*adapter.service.health())
                    return
                if not self._authorized():
                    return
                if path == "/v1/capabilities":
                    self._send(*adapter.service.capabilities())
                    return
                request_id = self._request_id(path)
                if request_id is not None:
                    status, body = adapter.service.poll(request_id)
                    headers = None
                    if body.get("status") in {"queued", "running"}:
                        headers = {"Retry-After": "0"}
                    self._send(status, body, headers=headers)
                    return
                self._not_found()

            def do_POST(self) -> None:
                if urlparse(self.path).path != "/v1/inference-jobs":
                    self._not_found()
                    return
                if not self._authorized():
                    return
                try:
                    request = InferenceRequest.from_dict(
                        self._request_json()
                    )
                except Exception as exc:
                    self._send(
                        HTTPStatus.BAD_REQUEST,
                        adapter.service._failure(
                            InferenceErrorCode.INVALID_REQUEST,
                            "request body is not a valid InferenceRequest",
                            retryable=False,
                            details={"error_type": type(exc).__name__},
                        ),
                    )
                    return
                status, body = adapter.service.submit(
                    request,
                    idempotency_key=self.headers.get("Idempotency-Key"),
                )
                headers = None
                if status == HTTPStatus.ACCEPTED:
                    headers = {
                        "Location": (
                            "/v1/inference-jobs/"
                            f"{body['request_id']}"
                        )
                    }
                self._send(status, body, headers=headers)

            def do_DELETE(self) -> None:
                if not self._authorized():
                    return
                request_id = self._request_id(urlparse(self.path).path)
                if request_id is None:
                    self._not_found()
                    return
                self._send(*adapter.service.cancel(request_id))

            def log_message(self, format, *args) -> None:
                return

            def _authorized(self) -> bool:
                if adapter.auth_token is None:
                    return True
                actual = self.headers.get("Authorization")
                expected = f"Bearer {adapter.auth_token}"
                if actual is not None and hmac.compare_digest(
                    actual,
                    expected,
                ):
                    return True
                self._send(
                    HTTPStatus.UNAUTHORIZED,
                    adapter.service._failure(
                        InferenceErrorCode.AUTHENTICATION_FAILED,
                        "authentication failed",
                        retryable=False,
                    ),
                )
                return False

            def _request_json(self) -> dict[str, object]:
                raw_length = self.headers.get("Content-Length")
                if raw_length is None:
                    raise ValueError("Content-Length is required")
                length = int(raw_length)
                if length < 1 or length > adapter.max_request_bytes:
                    raise ValueError("request body size is invalid")
                payload = json.loads(
                    self.rfile.read(length).decode("utf-8")
                )
                if not isinstance(payload, dict):
                    raise TypeError("request body must be an object")
                return payload

            @staticmethod
            def _request_id(path: str) -> str | None:
                prefix = "/v1/inference-jobs/"
                request_id = (
                    path[len(prefix):] if path.startswith(prefix) else ""
                )
                if not request_id or "/" in request_id:
                    return None
                return request_id

            def _not_found(self) -> None:
                self._send(
                    HTTPStatus.NOT_FOUND,
                    adapter.service._failure(
                        InferenceErrorCode.INVALID_REQUEST,
                        "route was not found",
                        retryable=False,
                        details={"reason": "ROUTE_NOT_FOUND"},
                    ),
                )

            def _send(
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
                self.send_header("Cache-Control", "no-store")
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(payload)

        return Handler
