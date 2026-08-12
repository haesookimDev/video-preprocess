"""Standard-library HTTP adapter for Pipeline REST API v1."""

from __future__ import annotations

import asyncio
import hmac
import json
import threading
from concurrent.futures import Future
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Coroutine
from urllib.parse import unquote, urlparse

from video_preprocess.services import (
    MediaNotFoundError,
    PipelineCapacityError,
    PipelineIdempotencyConflictError,
    PipelineRunNotFoundError,
    PipelineRunNotReadyError,
    PipelineRunService,
    PipelineRunSubmission,
    PipelineQueryRequest,
    QueryIndexNotFoundError,
    QueryRunNotReadyError,
    QueryService,
    QueryServiceInputError,
)


class _AsyncServiceRuntime:
    """Own one event loop shared by threaded HTTP request handlers."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._closed = False
        self._thread = threading.Thread(
            target=self._run,
            name="pipeline-application-runtime",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise RuntimeError("pipeline application runtime did not start")

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

    def call(self, coroutine: Coroutine, *, timeout_sec: float = 10.0):
        if self._closed:
            coroutine.close()
            raise RuntimeError("pipeline application runtime is closed")
        future: Future = asyncio.run_coroutine_threadsafe(
            coroutine,
            self._loop,
        )
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


class PipelineHTTPService:
    """Map transport-independent pipeline use cases to HTTP responses."""

    def __init__(
        self,
        run_service: PipelineRunService,
        *,
        query_service: QueryService | None = None,
        call_timeout_sec: float = 10.0,
        shutdown_timeout_sec: float = 5.0,
    ) -> None:
        for method_name in (
            "create",
            "get",
            "cancel",
            "artifacts",
        ):
            if not callable(getattr(run_service, method_name, None)):
                raise TypeError(f"run_service must implement {method_name}")
        for value, field_name in (
            (call_timeout_sec, "call_timeout_sec"),
            (shutdown_timeout_sec, "shutdown_timeout_sec"),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or value <= 0
            ):
                raise ValueError(f"{field_name} must be positive")
        self.run_service = run_service
        if query_service is not None and not callable(
            getattr(query_service, "query", None)
        ):
            raise TypeError("query_service must implement query or be None")
        self.query_service = query_service
        self.call_timeout_sec = float(call_timeout_sec)
        self.shutdown_timeout_sec = float(shutdown_timeout_sec)
        self._runtime = _AsyncServiceRuntime()

    def close(self) -> None:
        shutdown = getattr(self.run_service, "shutdown", None)
        if callable(shutdown):
            try:
                self._runtime.call(
                    shutdown(),
                    timeout_sec=self.shutdown_timeout_sec,
                )
            except Exception:
                pass
        self._runtime.close()

    def create(
        self,
        submission: PipelineRunSubmission,
        *,
        idempotency_key: str | None,
    ) -> tuple[int, dict[str, object], dict[str, str]]:
        if idempotency_key != submission.idempotency_key:
            return self._response(
                HTTPStatus.BAD_REQUEST,
                self.error(
                    "INVALID_REQUEST",
                    "Idempotency-Key must match request body",
                    retryable=False,
                ),
            )
        try:
            snapshot, created = self._runtime.call(
                self.run_service.create(submission),
                timeout_sec=self.call_timeout_sec,
            )
        except PipelineIdempotencyConflictError:
            return self._response(
                HTTPStatus.CONFLICT,
                self.error(
                    "IDEMPOTENCY_CONFLICT",
                    "idempotency key is bound to another request",
                    retryable=False,
                ),
            )
        except MediaNotFoundError:
            return self._response(
                HTTPStatus.BAD_REQUEST,
                self.error(
                    "MEDIA_NOT_FOUND",
                    "media_id is not available",
                    retryable=False,
                ),
            )
        except PipelineCapacityError:
            return self._response(
                HTTPStatus.TOO_MANY_REQUESTS,
                self.error(
                    "CAPACITY_EXCEEDED",
                    "pipeline run capacity is exhausted",
                    retryable=True,
                ),
                {"Retry-After": "1"},
            )
        except (TypeError, ValueError):
            return self._response(
                HTTPStatus.BAD_REQUEST,
                self.error(
                    "INVALID_REQUEST",
                    "pipeline request is invalid",
                    retryable=False,
                ),
            )
        except Exception:
            return self._response(
                HTTPStatus.SERVICE_UNAVAILABLE,
                self.error(
                    "SERVICE_UNAVAILABLE",
                    "pipeline service is unavailable",
                    retryable=True,
                ),
            )
        status = HTTPStatus.ACCEPTED if created else HTTPStatus.OK
        return self._response(
            status,
            snapshot.public_dict(),
            {"Location": f"/v1/pipeline-runs/{snapshot.run_id}"},
        )

    def get(self, run_id: str) -> tuple[int, dict[str, object], dict[str, str]]:
        try:
            snapshot = self._runtime.call(
                self._get(run_id),
                timeout_sec=self.call_timeout_sec,
            )
        except PipelineRunNotFoundError:
            return self._not_found()
        except (TypeError, ValueError):
            return self._invalid_run_id()
        except Exception:
            return self._unavailable()
        return self._response(HTTPStatus.OK, snapshot.public_dict())

    def cancel(
        self, run_id: str
    ) -> tuple[int, dict[str, object], dict[str, str]]:
        try:
            snapshot = self._runtime.call(
                self.run_service.cancel(run_id),
                timeout_sec=self.call_timeout_sec,
            )
        except PipelineRunNotFoundError:
            return self._not_found()
        except (TypeError, ValueError):
            return self._invalid_run_id()
        except Exception:
            return self._unavailable()
        return self._response(HTTPStatus.ACCEPTED, snapshot.public_dict())

    def artifacts(
        self, run_id: str
    ) -> tuple[int, dict[str, object], dict[str, str]]:
        try:
            body = self._runtime.call(
                self._artifacts(run_id),
                timeout_sec=self.call_timeout_sec,
            )
        except PipelineRunNotFoundError:
            return self._not_found()
        except PipelineRunNotReadyError:
            return self._response(
                HTTPStatus.CONFLICT,
                self.error(
                    "RUN_NOT_READY",
                    "pipeline artifacts are not ready",
                    retryable=True,
                ),
            )
        except (TypeError, ValueError):
            return self._invalid_run_id()
        except Exception:
            return self._unavailable()
        return self._response(HTTPStatus.OK, body)

    def query(
        self,
        request: PipelineQueryRequest,
    ) -> tuple[int, dict[str, object], dict[str, str]]:
        if self.query_service is None:
            return self._unavailable()
        try:
            result = self._runtime.call(
                self.query_service.query(request),
                timeout_sec=self.call_timeout_sec,
            )
        except PipelineRunNotFoundError:
            return self._not_found()
        except (QueryRunNotReadyError, QueryIndexNotFoundError):
            return self._response(
                HTTPStatus.CONFLICT,
                self.error(
                    "RUN_NOT_READY",
                    "pipeline run does not have a queryable index",
                    retryable=False,
                ),
            )
        except (QueryServiceInputError, TypeError, ValueError):
            return self._response(
                HTTPStatus.BAD_REQUEST,
                self.error(
                    "INVALID_REQUEST",
                    "query request or index is invalid",
                    retryable=False,
                ),
            )
        except Exception:
            return self._unavailable()
        return self._response(HTTPStatus.OK, result.to_dict())

    async def _get(self, run_id: str):
        return self.run_service.get(run_id)

    async def _artifacts(self, run_id: str):
        return self.run_service.artifacts(run_id)

    @staticmethod
    def error(
        code: str,
        message: str,
        *,
        retryable: bool,
    ) -> dict[str, object]:
        return {
            "schema_version": "1",
            "error": {
                "code": code,
                "message": message,
                "retryable": retryable,
            },
        }

    @staticmethod
    def _response(
        status: int,
        body: dict[str, object],
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, object], dict[str, str]]:
        return int(status), body, {} if headers is None else headers

    def _not_found(self):
        return self._response(
            HTTPStatus.NOT_FOUND,
            self.error(
                "RUN_NOT_FOUND",
                "pipeline run was not found",
                retryable=False,
            ),
        )

    def _invalid_run_id(self):
        return self._response(
            HTTPStatus.BAD_REQUEST,
            self.error(
                "INVALID_REQUEST",
                "run_id is invalid",
                retryable=False,
            ),
        )

    def _unavailable(self):
        return self._response(
            HTTPStatus.SERVICE_UNAVAILABLE,
            self.error(
                "SERVICE_UNAVAILABLE",
                "pipeline service is unavailable",
                retryable=True,
            ),
        )


class _RequestBodyError(ValueError):
    pass


class _RequestBodyTooLarge(_RequestBodyError):
    pass


class PipelineHTTPServer:
    """Bind PipelineRunService to a threaded stdlib HTTP server."""

    def __init__(
        self,
        *,
        run_service: PipelineRunService,
        query_service: QueryService | None = None,
        host: str = "127.0.0.1",
        port: int = 8090,
        auth_token: str | None = None,
        max_request_bytes: int = 1024 * 1024,
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
        self.auth_token = None if auth_token is None else auth_token.strip()
        self.max_request_bytes = max_request_bytes
        self.service = PipelineHTTPService(
            run_service,
            query_service=query_service,
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
            raise RuntimeError("pipeline HTTP server is already started")
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="pipeline-http-server",
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

    def __enter__(self) -> "PipelineHTTPServer":
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def _handler_type(self):
        adapter = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "VideoPreprocessPipeline/1"

            def do_GET(self) -> None:
                if not self._authorized():
                    return
                route = self._run_route(urlparse(self.path).path)
                if route is None:
                    self._not_found()
                    return
                run_id, action = route
                if action is None:
                    self._send(*adapter.service.get(run_id))
                    return
                if action == "artifacts":
                    self._send(*adapter.service.artifacts(run_id))
                    return
                self._not_found()

            def do_POST(self) -> None:
                if not self._authorized():
                    return
                path = urlparse(self.path).path
                route = self._run_route(path)
                is_create = path == "/v1/pipeline-runs"
                is_query = route is not None and route[1] == "queries"
                if not is_create and not is_query:
                    self._not_found()
                    return
                try:
                    payload = self._request_json()
                    if is_create:
                        request = PipelineRunSubmission.from_dict(payload)
                    else:
                        request = PipelineQueryRequest.from_dict(
                            route[0], payload
                        )
                except _RequestBodyTooLarge:
                    self._send(
                        HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                        adapter.service.error(
                            "PAYLOAD_TOO_LARGE",
                            "request body exceeds configured limit",
                            retryable=False,
                        ),
                        {},
                    )
                    return
                except Exception:
                    self._send(
                        HTTPStatus.BAD_REQUEST,
                        adapter.service.error(
                            "INVALID_REQUEST",
                            "request body is invalid",
                            retryable=False,
                        ),
                        {},
                    )
                    return
                if is_create:
                    self._send(
                        *adapter.service.create(
                            request,
                            idempotency_key=self.headers.get(
                                "Idempotency-Key"
                            ),
                        )
                    )
                    return
                self._send(*adapter.service.query(request))

            def do_DELETE(self) -> None:
                if not self._authorized():
                    return
                route = self._run_route(urlparse(self.path).path)
                if route is None or route[1] is not None:
                    self._not_found()
                    return
                self._send(*adapter.service.cancel(route[0]))

            def log_message(self, format, *args) -> None:
                return

            def _authorized(self) -> bool:
                if adapter.auth_token is None:
                    return True
                actual = self.headers.get("Authorization")
                expected = f"Bearer {adapter.auth_token}"
                if actual is not None and hmac.compare_digest(actual, expected):
                    return True
                self._send(
                    HTTPStatus.UNAUTHORIZED,
                    adapter.service.error(
                        "UNAUTHORIZED",
                        "authentication failed",
                        retryable=False,
                    ),
                    {},
                )
                return False

            def _request_json(self) -> dict[str, object]:
                content_type = self.headers.get("Content-Type", "")
                if content_type.split(";", 1)[0].strip() != "application/json":
                    raise _RequestBodyError("Content-Type must be JSON")
                raw_length = self.headers.get("Content-Length")
                if raw_length is None:
                    raise _RequestBodyError("Content-Length is required")
                try:
                    length = int(raw_length)
                except ValueError as exc:
                    raise _RequestBodyError("Content-Length is invalid") from exc
                if length > adapter.max_request_bytes:
                    raise _RequestBodyTooLarge("request body is too large")
                if length < 1:
                    raise _RequestBodyError("request body is empty")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise _RequestBodyError("request body must be an object")
                return payload

            @staticmethod
            def _run_route(path: str) -> tuple[str, str | None] | None:
                prefix = "/v1/pipeline-runs/"
                remainder = path[len(prefix):] if path.startswith(prefix) else ""
                parts = remainder.split("/") if remainder else []
                if len(parts) not in {1, 2}:
                    return None
                run_id = unquote(parts[0])
                if not run_id or "/" in run_id or "\\" in run_id:
                    return None
                action = parts[1] if len(parts) == 2 else None
                return run_id, action

            def _not_found(self) -> None:
                self._send(
                    HTTPStatus.NOT_FOUND,
                    adapter.service.error(
                        "RUN_NOT_FOUND",
                        "route was not found",
                        retryable=False,
                    ),
                    {},
                )

            def _send(
                self,
                status: int,
                body: dict[str, object],
                headers: dict[str, str],
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
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(payload)

        return Handler
