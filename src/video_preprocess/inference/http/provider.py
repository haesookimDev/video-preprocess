"""Asynchronous HTTP implementation of the common Inference Provider port."""

from __future__ import annotations

import asyncio
import json
import math
import random
import threading
import time
from collections.abc import Awaitable, Callable, Collection, Iterator, Mapping
from dataclasses import dataclass, replace
from typing import Protocol
from urllib.parse import urlparse

from video_preprocess.domain import (
    ArtifactRef,
    EffectiveModel,
    InferenceErrorCode,
    InferenceFailure,
    InferenceRequest,
    InferenceResponse,
    InferenceStatus,
    ProviderCapabilities,
    ProviderHealth,
)
from video_preprocess.inference.errors import InferenceCallError

from .transport import HTTPTransportResponse, UrllibHTTPTransport


class HTTPTransport(Protocol):
    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_sec: float,
    ) -> HTTPTransportResponse: ...


Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]
RandomValue = Callable[[], float]


@dataclass(frozen=True, slots=True)
class HTTPRetryPolicy:
    """Bounded HTTP retry and circuit-breaker settings."""

    max_attempts: int = 3
    initial_backoff_sec: float = 0.1
    backoff_multiplier: float = 2.0
    max_backoff_sec: float = 2.0
    jitter_ratio: float = 0.2
    circuit_failure_threshold: int = 5
    circuit_recovery_sec: float = 30.0

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or self.max_attempts < 1
        ):
            raise ValueError("max_attempts must be a positive integer")
        if (
            isinstance(self.circuit_failure_threshold, bool)
            or not isinstance(self.circuit_failure_threshold, int)
            or self.circuit_failure_threshold < 1
        ):
            raise ValueError(
                "circuit_failure_threshold must be a positive integer"
            )
        for field_name, minimum in (
            ("initial_backoff_sec", 0.0),
            ("backoff_multiplier", 1.0),
            ("max_backoff_sec", 0.0),
            ("jitter_ratio", 0.0),
            ("circuit_recovery_sec", 0.001),
        ):
            value = getattr(self, field_name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value < minimum
            ):
                raise ValueError(f"{field_name} must be at least {minimum}")
            object.__setattr__(self, field_name, float(value))
        if self.jitter_ratio > 1:
            raise ValueError("jitter_ratio must not exceed 1")
        if self.max_backoff_sec < self.initial_backoff_sec:
            raise ValueError(
                "max_backoff_sec must be at least initial_backoff_sec"
            )

    def backoff_sec(
        self,
        *,
        attempts_used: int,
        random_value: float,
    ) -> float:
        if (
            isinstance(random_value, bool)
            or not isinstance(random_value, (int, float))
            or not math.isfinite(float(random_value))
            or not 0 <= random_value <= 1
        ):
            raise ValueError("random_value must be between 0 and 1")
        base = min(
            self.initial_backoff_sec
            * (self.backoff_multiplier ** (attempts_used - 1)),
            self.max_backoff_sec,
        )
        jitter = base * self.jitter_ratio * ((2 * random_value) - 1)
        return max(0.0, min(base + jitter, self.max_backoff_sec))


@dataclass(frozen=True, slots=True)
class _JSONResponse:
    status: int
    headers: Mapping[str, str]
    body: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class _JobSnapshot:
    request_id: str
    status: str
    response: InferenceResponse | None
    retry_after_sec: float | None

    @property
    def terminal(self) -> bool:
        return self.status in {"succeeded", "failed", "cancelled"}


class _CircuitBreaker:
    def __init__(self, policy: HTTPRetryPolicy, clock: Clock) -> None:
        self.policy = policy
        self.clock = clock
        self._failures = 0
        self._open_until: float | None = None
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            if self._open_until is None:
                return True
            if self.clock() < self._open_until:
                return False
            self._failures = 0
            self._open_until = None
            return True

    def success(self) -> None:
        with self._lock:
            self._failures = 0
            self._open_until = None

    def failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.policy.circuit_failure_threshold:
                self._open_until = (
                    self.clock() + self.policy.circuit_recovery_sec
                )


class HTTPInferenceProvider:
    """Submit, poll, and cancel one model alias through HTTP Inference v1."""

    RETRYABLE_STATUSES = frozenset({408, 429, 502, 503, 504})
    BREAKER_STATUSES = frozenset({502, 503, 504})

    def __init__(
        self,
        *,
        alias: str,
        endpoint: str,
        auth_token: str | None = None,
        allowed_artifact_namespaces: Collection[str] = (),
        operation_timeout_sec: float = 10.0,
        poll_interval_sec: float = 0.1,
        max_poll_interval_sec: float = 2.0,
        capability_ttl_sec: float = 30.0,
        retry_policy: HTTPRetryPolicy | None = None,
        transport: HTTPTransport | None = None,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        random_value: RandomValue = random.random,
    ) -> None:
        self.alias = self._required_string(alias, "alias")
        self.endpoint = self._normalize_endpoint(endpoint)
        if auth_token is not None:
            self._required_string(auth_token, "auth_token")
        self._auth_token = auth_token
        if isinstance(allowed_artifact_namespaces, (str, bytes)):
            raise TypeError(
                "allowed_artifact_namespaces must be a collection"
            )
        self.allowed_artifact_namespaces = frozenset(
            self._required_string(namespace, "artifact namespace")
            for namespace in allowed_artifact_namespaces
        )
        self.operation_timeout_sec = self._positive_number(
            operation_timeout_sec,
            "operation_timeout_sec",
        )
        self.poll_interval_sec = self._positive_number(
            poll_interval_sec,
            "poll_interval_sec",
        )
        self.max_poll_interval_sec = self._positive_number(
            max_poll_interval_sec,
            "max_poll_interval_sec",
        )
        if self.max_poll_interval_sec < self.poll_interval_sec:
            raise ValueError(
                "max_poll_interval_sec must be at least poll_interval_sec"
            )
        self.capability_ttl_sec = self._positive_number(
            capability_ttl_sec,
            "capability_ttl_sec",
        )
        self.retry_policy = retry_policy or HTTPRetryPolicy()
        if not isinstance(self.retry_policy, HTTPRetryPolicy):
            raise TypeError("retry_policy must be an HTTPRetryPolicy")
        self.transport = transport or UrllibHTTPTransport()
        if not callable(getattr(self.transport, "request", None)):
            raise TypeError("transport must implement request")
        for callback, field_name in (
            (clock, "clock"),
            (sleep, "sleep"),
            (random_value, "random_value"),
        ):
            if not callable(callback):
                raise TypeError(f"{field_name} must be callable")
        self._clock = clock
        self._sleep = sleep
        self._random_value = random_value
        self._breaker = _CircuitBreaker(self.retry_policy, clock)
        self._capabilities: ProviderCapabilities | None = None
        self._capabilities_expires_at = 0.0
        self._active_jobs: dict[str, str] = {}

    async def capabilities(self) -> ProviderCapabilities:
        if (
            self._capabilities is not None
            and self._clock() < self._capabilities_expires_at
        ):
            return self._capabilities
        response = await self._request_json(
            "GET",
            "/v1/capabilities",
            expected_statuses={200},
            deadline=self._clock() + self.operation_timeout_sec,
        )
        try:
            capabilities = ProviderCapabilities.from_dict(response.body)
        except Exception as exc:
            raise self._call_error(
                InferenceErrorCode.PROVIDER_UNAVAILABLE,
                "provider returned invalid capabilities",
                retryable=False,
                details={"error_type": type(exc).__name__},
            ) from exc
        self._capabilities = capabilities
        self._capabilities_expires_at = (
            self._clock() + self.capability_ttl_sec
        )
        return capabilities

    async def health(self) -> ProviderHealth:
        response = await self._request_json(
            "GET",
            "/v1/health",
            expected_statuses={200},
            deadline=self._clock() + self.operation_timeout_sec,
        )
        try:
            return ProviderHealth.from_dict(response.body)
        except Exception as exc:
            raise self._call_error(
                InferenceErrorCode.PROVIDER_UNAVAILABLE,
                "provider returned invalid health",
                retryable=False,
                details={"error_type": type(exc).__name__},
            ) from exc

    async def effective_model(self) -> EffectiveModel | None:
        capabilities = await self.capabilities()
        return capabilities.effective_models.get(self.alias)

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        if not isinstance(request, InferenceRequest):
            raise TypeError("request must be an InferenceRequest")
        if request.model.alias != self.alias:
            return self._failure_response(
                request,
                InferenceErrorCode.UNSUPPORTED_CAPABILITY,
                "request alias does not match HTTP provider binding",
                retryable=False,
            )
        artifact_failure = self._validate_artifact_namespaces(request)
        if artifact_failure is not None:
            return artifact_failure
        deadline = self._clock() + request.timeout_sec
        remote_request_id = request.request_id
        try:
            submitted = await self._request_json(
                "POST",
                "/v1/inference-jobs",
                expected_statuses={200, 202},
                body=request.to_dict(),
                idempotency_key=request.idempotency_key,
                request_id=request.request_id,
                deadline=deadline,
            )
            remote_request_id = self._job_request_id(
                submitted.body,
                request_id=request.request_id,
            )
            self._active_jobs[request.request_id] = remote_request_id
            job = self._parse_job(
                submitted.body,
                expected_request_id=remote_request_id,
            )
            while not job.terminal:
                await self._poll_delay(job, submitted.headers, deadline)
                submitted = await self._request_json(
                    "GET",
                    f"/v1/inference-jobs/{remote_request_id}",
                    expected_statuses={200},
                    request_id=remote_request_id,
                    deadline=deadline,
                )
                job = self._parse_job(
                    submitted.body,
                    expected_request_id=remote_request_id,
                )
            if job.response is None:
                raise self._call_error(
                    InferenceErrorCode.PROVIDER_UNAVAILABLE,
                    "terminal inference job has no response",
                    retryable=False,
                    request_id=remote_request_id,
                )
            return self._rebind_response(request, job.response)
        except InferenceCallError as exc:
            if exc.failure.code is InferenceErrorCode.PROVIDER_TIMEOUT:
                await self._cancel_best_effort(remote_request_id)
            return self._response_from_failure(request, exc.failure)
        except asyncio.CancelledError:
            await asyncio.shield(
                self._cancel_best_effort(remote_request_id)
            )
            raise
        finally:
            self._active_jobs.pop(request.request_id, None)

    async def cancel(self, request_id: str) -> None:
        normalized = self._required_string(request_id, "request_id")
        remote_request_id = self._active_jobs.get(normalized, normalized)
        try:
            await self._request_json(
                "DELETE",
                f"/v1/inference-jobs/{remote_request_id}",
                expected_statuses={200, 202, 404},
                request_id=remote_request_id,
                deadline=self._clock() + self.operation_timeout_sec,
            )
        except InferenceCallError as exc:
            if exc.failure.details.get("reason") == "JOB_NOT_FOUND":
                return
            raise

    async def _cancel_best_effort(self, request_id: str) -> None:
        try:
            await self.cancel(request_id)
        except Exception:
            return

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        expected_statuses: Collection[int],
        deadline: float,
        body: Mapping[str, object] | None = None,
        idempotency_key: str | None = None,
        request_id: str | None = None,
    ) -> _JSONResponse:
        encoded = None
        if body is not None:
            encoded = json.dumps(
                body,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "User-Agent": "video-preprocess-http-inference/1",
        }
        if encoded is not None:
            headers["Content-Type"] = "application/json"
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        if self._auth_token is not None:
            headers["Authorization"] = f"Bearer {self._auth_token}"

        last_failure = None
        for attempt in range(1, self.retry_policy.max_attempts + 1):
            if not self._breaker.allow():
                raise self._call_error(
                    InferenceErrorCode.PROVIDER_UNAVAILABLE,
                    "HTTP inference circuit is open",
                    retryable=True,
                    request_id=request_id,
                    details={"reason": "CIRCUIT_OPEN"},
                )
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise self._timeout_error(request_id)
            timeout_sec = min(self.operation_timeout_sec, remaining)
            try:
                response = await asyncio.wait_for(
                    self.transport.request(
                        method,
                        self.endpoint + path,
                        headers=headers,
                        body=encoded,
                        timeout_sec=timeout_sec,
                    ),
                    timeout=timeout_sec,
                )
            except asyncio.CancelledError:
                raise
            except (asyncio.TimeoutError, TimeoutError) as exc:
                self._breaker.failure()
                last_failure = self._timeout_error(request_id)
                if attempt >= self.retry_policy.max_attempts:
                    raise last_failure from exc
                await self._retry_delay(
                    attempt,
                    deadline=deadline,
                    retry_after_sec=None,
                    request_id=request_id,
                )
                continue
            except Exception as exc:
                self._breaker.failure()
                last_failure = self._call_error(
                    InferenceErrorCode.PROVIDER_UNAVAILABLE,
                    "HTTP inference transport failed",
                    retryable=True,
                    request_id=request_id,
                    details={"error_type": type(exc).__name__},
                )
                if attempt >= self.retry_policy.max_attempts:
                    raise last_failure from exc
                await self._retry_delay(
                    attempt,
                    deadline=deadline,
                    retry_after_sec=None,
                    request_id=request_id,
                )
                continue

            if response.status in expected_statuses:
                parsed = self._parse_json_body(
                    response,
                    request_id=request_id,
                )
                self._breaker.success()
                return _JSONResponse(
                    status=response.status,
                    headers=response.headers,
                    body=parsed,
                )

            try:
                parsed = self._parse_json_body(
                    response,
                    request_id=request_id,
                )
            except InferenceCallError:
                parsed = {}
            last_failure = self._http_failure(
                response.status,
                parsed,
                request_id=request_id,
            )
            if response.status in self.BREAKER_STATUSES:
                self._breaker.failure()
            else:
                self._breaker.success()
            if (
                not last_failure.failure.retryable
                or response.status not in self.RETRYABLE_STATUSES
                or attempt >= self.retry_policy.max_attempts
            ):
                raise last_failure
            await self._retry_delay(
                attempt,
                deadline=deadline,
                retry_after_sec=self._retry_after(response.headers),
                request_id=request_id,
            )

        if last_failure is None:
            raise RuntimeError("HTTP retry loop produced no result")
        raise last_failure

    async def _retry_delay(
        self,
        attempts_used: int,
        *,
        deadline: float,
        retry_after_sec: float | None,
        request_id: str | None,
    ) -> None:
        delay = (
            retry_after_sec
            if retry_after_sec is not None
            else self.retry_policy.backoff_sec(
                attempts_used=attempts_used,
                random_value=self._random_value(),
            )
        )
        remaining = deadline - self._clock()
        if remaining <= 0 or delay >= remaining:
            raise self._timeout_error(request_id)
        if delay > 0:
            await self._sleep(delay)

    async def _poll_delay(
        self,
        job: _JobSnapshot,
        headers: Mapping[str, str],
        deadline: float,
    ) -> None:
        header_delay = self._retry_after(headers)
        delay = job.retry_after_sec
        if delay is None:
            delay = header_delay
        if delay is None:
            delay = self.poll_interval_sec
        delay = min(max(delay, self.poll_interval_sec), self.max_poll_interval_sec)
        remaining = deadline - self._clock()
        if remaining <= 0 or delay >= remaining:
            raise self._timeout_error(job.request_id)
        await self._sleep(delay)

    @staticmethod
    def _parse_json_body(
        response: HTTPTransportResponse,
        *,
        request_id: str | None,
    ) -> Mapping[str, object]:
        try:
            parsed = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPInferenceProvider._call_error(
                InferenceErrorCode.PROVIDER_UNAVAILABLE,
                "HTTP inference response is not valid JSON",
                retryable=False,
                request_id=request_id,
                details={"error_type": type(exc).__name__},
            ) from exc
        if not isinstance(parsed, Mapping):
            raise HTTPInferenceProvider._call_error(
                InferenceErrorCode.PROVIDER_UNAVAILABLE,
                "HTTP inference response must be a JSON object",
                retryable=False,
                request_id=request_id,
            )
        return parsed

    @classmethod
    def _http_failure(
        cls,
        status: int,
        body: Mapping[str, object],
        *,
        request_id: str | None,
    ) -> InferenceCallError:
        try:
            failure = InferenceFailure.from_dict(body)
        except Exception:
            failure = cls._fallback_http_failure(status, request_id)
        if failure.request_id not in {None, request_id}:
            failure = InferenceFailure(
                code=InferenceErrorCode.PROVIDER_UNAVAILABLE,
                message="HTTP failure request_id does not match request",
                retryable=False,
                request_id=request_id,
            )
        elif request_id is not None and failure.request_id is None:
            failure = replace(failure, request_id=request_id)
        return InferenceCallError(failure)

    @staticmethod
    def _fallback_http_failure(
        status: int,
        request_id: str | None,
    ) -> InferenceFailure:
        if status == 401:
            code = InferenceErrorCode.AUTHENTICATION_FAILED
            retryable = False
        elif status == 403:
            code = InferenceErrorCode.MODEL_ACCESS_DENIED
            retryable = False
        elif status == 429:
            code = InferenceErrorCode.PROVIDER_RATE_LIMITED
            retryable = True
        elif status in {408, 504}:
            code = InferenceErrorCode.PROVIDER_TIMEOUT
            retryable = True
        elif status in {400, 409}:
            code = InferenceErrorCode.INVALID_REQUEST
            retryable = False
        elif status == 413:
            code = InferenceErrorCode.UNSUPPORTED_CAPABILITY
            retryable = False
        else:
            code = InferenceErrorCode.PROVIDER_UNAVAILABLE
            retryable = status >= 500
        return InferenceFailure(
            code=code,
            message="HTTP inference request failed",
            retryable=retryable,
            details={"http_status": status},
            request_id=request_id,
        )

    @staticmethod
    def _job_request_id(
        body: Mapping[str, object],
        *,
        request_id: str,
    ) -> str:
        remote_request_id = body.get("request_id")
        if not isinstance(remote_request_id, str) or not remote_request_id:
            raise HTTPInferenceProvider._call_error(
                InferenceErrorCode.PROVIDER_UNAVAILABLE,
                "inference job has an invalid request_id",
                retryable=False,
                request_id=request_id,
            )
        return remote_request_id

    @staticmethod
    def _parse_job(
        body: Mapping[str, object],
        *,
        expected_request_id: str,
    ) -> _JobSnapshot:
        request_id = body.get("request_id")
        status = body.get("status")
        if request_id != expected_request_id:
            raise HTTPInferenceProvider._call_error(
                InferenceErrorCode.PROVIDER_UNAVAILABLE,
                "inference job request_id does not match request",
                retryable=False,
                request_id=expected_request_id,
            )
        if status not in {
            "queued",
            "running",
            "succeeded",
            "failed",
            "cancelled",
        }:
            raise HTTPInferenceProvider._call_error(
                InferenceErrorCode.PROVIDER_UNAVAILABLE,
                "inference job has an invalid status",
                retryable=False,
                request_id=expected_request_id,
            )
        raw_response = body.get("response")
        response = None
        if status in {"succeeded", "failed", "cancelled"}:
            if not isinstance(raw_response, Mapping):
                raise HTTPInferenceProvider._call_error(
                    InferenceErrorCode.PROVIDER_UNAVAILABLE,
                    "terminal inference job has no response",
                    retryable=False,
                    request_id=expected_request_id,
                )
            try:
                response = InferenceResponse.from_dict(raw_response)
            except Exception as exc:
                raise HTTPInferenceProvider._call_error(
                    InferenceErrorCode.PROVIDER_UNAVAILABLE,
                    "terminal inference response is invalid",
                    retryable=False,
                    request_id=expected_request_id,
                    details={"error_type": type(exc).__name__},
                ) from exc
            if response.request_id != expected_request_id:
                raise HTTPInferenceProvider._call_error(
                    InferenceErrorCode.PROVIDER_UNAVAILABLE,
                    "terminal response request_id does not match request",
                    retryable=False,
                    request_id=expected_request_id,
                )
            if response.status.value != status:
                raise HTTPInferenceProvider._call_error(
                    InferenceErrorCode.PROVIDER_UNAVAILABLE,
                    "job and response status do not match",
                    retryable=False,
                    request_id=expected_request_id,
                )
        elif raw_response is not None:
            raise HTTPInferenceProvider._call_error(
                InferenceErrorCode.PROVIDER_UNAVAILABLE,
                "non-terminal inference job contains a response",
                retryable=False,
                request_id=expected_request_id,
            )
        retry_after = body.get("retry_after_sec")
        if retry_after is not None:
            if (
                isinstance(retry_after, bool)
                or not isinstance(retry_after, (int, float))
                or not math.isfinite(float(retry_after))
                or retry_after < 0
            ):
                raise HTTPInferenceProvider._call_error(
                    InferenceErrorCode.PROVIDER_UNAVAILABLE,
                    "job retry_after_sec is invalid",
                    retryable=False,
                    request_id=expected_request_id,
                )
            retry_after = float(retry_after)
        return _JobSnapshot(
            request_id=expected_request_id,
            status=status,
            response=response,
            retry_after_sec=retry_after,
        )

    def _validate_artifact_namespaces(
        self,
        request: InferenceRequest,
    ) -> InferenceResponse | None:
        for field_name, artifact in self._iter_artifacts(
            request.inputs,
            "inputs",
        ):
            namespace = urlparse(artifact.uri).netloc
            if namespace not in self.allowed_artifact_namespaces:
                return self._failure_response(
                    request,
                    InferenceErrorCode.INVALID_REQUEST,
                    "artifact namespace is not allowed for HTTP inference",
                    retryable=False,
                    details={"input": field_name},
                )
        return None

    @classmethod
    def _iter_artifacts(
        cls,
        value: object,
        field_name: str,
    ) -> Iterator[tuple[str, ArtifactRef]]:
        if isinstance(value, ArtifactRef):
            yield field_name, value
        elif isinstance(value, list):
            for index, item in enumerate(value):
                yield from cls._iter_artifacts(
                    item,
                    f"{field_name}[{index}]",
                )
        elif isinstance(value, Mapping):
            for key, item in value.items():
                yield from cls._iter_artifacts(
                    item,
                    f"{field_name}.{key}",
                )

    @staticmethod
    def _retry_after(headers: Mapping[str, str]) -> float | None:
        raw = next(
            (
                value
                for name, value in headers.items()
                if name.lower() == "retry-after"
            ),
            None,
        )
        if raw is None:
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value) or value < 0:
            return None
        return value

    @staticmethod
    def _failure_response(
        request: InferenceRequest,
        code: InferenceErrorCode,
        message: str,
        *,
        retryable: bool,
        details: Mapping[str, object] | None = None,
    ) -> InferenceResponse:
        return InferenceResponse(
            request_id=request.request_id,
            status=(
                InferenceStatus.CANCELLED
                if code is InferenceErrorCode.CANCELLED
                else InferenceStatus.FAILED
            ),
            error=InferenceFailure(
                code=code,
                message=message,
                retryable=retryable,
                details={} if details is None else details,
                request_id=request.request_id,
            ),
        )

    @classmethod
    def _response_from_failure(
        cls,
        request: InferenceRequest,
        failure: InferenceFailure,
    ) -> InferenceResponse:
        normalized = replace(failure, request_id=request.request_id)
        return InferenceResponse(
            request_id=request.request_id,
            status=(
                InferenceStatus.CANCELLED
                if normalized.code is InferenceErrorCode.CANCELLED
                else InferenceStatus.FAILED
            ),
            error=normalized,
        )

    @staticmethod
    def _rebind_response(
        request: InferenceRequest,
        response: InferenceResponse,
    ) -> InferenceResponse:
        error = response.error
        if error is not None:
            error = replace(error, request_id=request.request_id)
        return replace(
            response,
            request_id=request.request_id,
            error=error,
        )

    @staticmethod
    def _call_error(
        code: InferenceErrorCode,
        message: str,
        *,
        retryable: bool,
        request_id: str | None = None,
        details: Mapping[str, object] | None = None,
    ) -> InferenceCallError:
        return InferenceCallError(
            InferenceFailure(
                code=code,
                message=message,
                retryable=retryable,
                details={} if details is None else details,
                request_id=request_id,
            )
        )

    @classmethod
    def _timeout_error(
        cls,
        request_id: str | None,
    ) -> InferenceCallError:
        return cls._call_error(
            InferenceErrorCode.PROVIDER_TIMEOUT,
            "HTTP inference deadline elapsed",
            retryable=True,
            request_id=request_id,
        )

    @staticmethod
    def _required_string(value: object, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} must be a non-empty string")
        return value.strip()

    @staticmethod
    def _positive_number(value: object, field_name: str) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value <= 0
        ):
            raise ValueError(f"{field_name} must be positive")
        return float(value)

    @classmethod
    def _normalize_endpoint(cls, endpoint: object) -> str:
        value = cls._required_string(endpoint, "endpoint").rstrip("/")
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("endpoint must be an absolute HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("endpoint must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("endpoint must not contain query or fragment")
        return value
