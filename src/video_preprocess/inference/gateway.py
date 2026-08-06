"""Alias-based inference routing with timeout and error normalization."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping

from video_preprocess.domain import (
    ArtifactRef,
    InferenceErrorCode,
    InferenceFailure,
    InferenceRequest,
    InferenceResponse,
    InferenceStatus,
    ProviderCapabilities,
    ProviderHealth,
)

from .errors import ProviderConfigurationError
from .provider import InferenceProvider


class InferenceGateway:
    """Routes a model alias without exposing provider type to callers."""

    def __init__(self, bindings: Mapping[str, InferenceProvider]) -> None:
        normalized = {}
        for alias, provider in bindings.items():
            if not isinstance(alias, str) or not alias.strip():
                raise ProviderConfigurationError(
                    "provider alias must be a non-empty string"
                )
            if alias in normalized:
                raise ProviderConfigurationError(
                    f"duplicate provider alias: {alias}"
                )
            normalized[alias] = provider
        self._bindings = normalized
        self._active: dict[str, InferenceProvider] = {}

    @property
    def aliases(self) -> tuple[str, ...]:
        return tuple(self._bindings)

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        if not isinstance(request, InferenceRequest):
            raise TypeError("request must be an InferenceRequest")
        provider = self._bindings.get(request.model.alias)
        if provider is None:
            return self._failure(
                request,
                InferenceErrorCode.UNSUPPORTED_CAPABILITY,
                f"model alias is not bound: {request.model.alias}",
                retryable=False,
            )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + request.timeout_sec
        try:
            capabilities = await asyncio.wait_for(
                provider.capabilities(),
                timeout=max(deadline - loop.time(), 0.001),
            )
        except asyncio.TimeoutError:
            return self._failure(
                request,
                InferenceErrorCode.PROVIDER_TIMEOUT,
                "provider capability check timed out",
                retryable=True,
            )
        except Exception as exc:
            return self._unexpected_failure(request, exc)

        if not isinstance(capabilities, ProviderCapabilities):
            return self._failure(
                request,
                InferenceErrorCode.PROVIDER_UNAVAILABLE,
                "provider returned invalid capabilities",
                retryable=False,
            )

        capability_error = self._validate_capabilities(
            request, capabilities
        )
        if capability_error is not None:
            return capability_error

        self._active[request.request_id] = provider
        try:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return self._failure(
                    request,
                    InferenceErrorCode.PROVIDER_TIMEOUT,
                    "inference deadline elapsed during capability check",
                    retryable=True,
                )
            response = await asyncio.wait_for(
                provider.infer(request),
                timeout=remaining,
            )
        except asyncio.TimeoutError:
            return self._failure(
                request,
                InferenceErrorCode.PROVIDER_TIMEOUT,
                "inference did not finish before timeout",
                retryable=True,
            )
        except Exception as exc:
            return self._unexpected_failure(request, exc)
        finally:
            self._active.pop(request.request_id, None)

        if not isinstance(response, InferenceResponse):
            return self._failure(
                request,
                InferenceErrorCode.PROVIDER_UNAVAILABLE,
                "provider returned an invalid response type",
                retryable=False,
            )
        if response.request_id != request.request_id:
            return self._failure(
                request,
                InferenceErrorCode.PROVIDER_UNAVAILABLE,
                "provider response request_id does not match request",
                retryable=False,
            )
        return response

    async def capabilities(self, alias: str) -> ProviderCapabilities:
        provider = self._provider(alias)
        return await provider.capabilities()

    async def health(self, alias: str) -> ProviderHealth:
        provider = self._provider(alias)
        return await provider.health()

    async def cancel(self, request_id: str) -> bool:
        provider = self._active.get(request_id)
        if provider is None:
            return False
        capabilities = await provider.capabilities()
        if not capabilities.supports_cancellation:
            return False
        await provider.cancel(request_id)
        return True

    def _provider(self, alias: str) -> InferenceProvider:
        provider = self._bindings.get(alias)
        if provider is None:
            raise ProviderConfigurationError(f"unknown model alias: {alias}")
        return provider

    def _validate_capabilities(
        self,
        request: InferenceRequest,
        capabilities: ProviderCapabilities,
    ) -> InferenceResponse | None:
        if request.model.alias not in capabilities.model_aliases:
            return self._failure(
                request,
                InferenceErrorCode.UNSUPPORTED_CAPABILITY,
                "provider does not declare the requested model alias",
                retryable=False,
            )
        if request.task not in capabilities.tasks:
            return self._failure(
                request,
                InferenceErrorCode.UNSUPPORTED_CAPABILITY,
                f"provider does not support task {request.task.value}",
                retryable=False,
            )
        batch_size = self._batch_size(request)
        if (
            batch_size is not None
            and batch_size > capabilities.max_batch_size
        ):
            return self._failure(
                request,
                InferenceErrorCode.UNSUPPORTED_CAPABILITY,
                "request batch exceeds provider capability",
                retryable=False,
                details={"max_batch_size": capabilities.max_batch_size},
            )
        if capabilities.max_artifact_bytes is not None:
            for input_name, input_value in request.inputs.items():
                for artifact_name, artifact in self._iter_artifacts(
                    input_value,
                    input_name,
                ):
                    if artifact.size_bytes > capabilities.max_artifact_bytes:
                        return self._failure(
                            request,
                            InferenceErrorCode.UNSUPPORTED_CAPABILITY,
                            (
                                "input artifact exceeds provider limit: "
                                f"{artifact_name}"
                            ),
                            retryable=False,
                            details={
                                "max_artifact_bytes": (
                                    capabilities.max_artifact_bytes
                                )
                            },
                        )
        return None

    @staticmethod
    def _batch_size(request: InferenceRequest) -> int | None:
        for input_name in ("texts", "images", "chunks"):
            value = request.inputs.get(input_name)
            if isinstance(value, list):
                return len(value)
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
    def _failure(
        request: InferenceRequest,
        code: InferenceErrorCode,
        message: str,
        *,
        retryable: bool,
        details: Mapping[str, object] | None = None,
    ) -> InferenceResponse:
        return InferenceResponse(
            request_id=request.request_id,
            status=InferenceStatus.FAILED,
            error=InferenceFailure(
                code=code,
                message=message,
                retryable=retryable,
                details={} if details is None else details,
                request_id=request.request_id,
            ),
        )

    @classmethod
    def _unexpected_failure(
        cls,
        request: InferenceRequest,
        exc: Exception,
    ) -> InferenceResponse:
        return cls._failure(
            request,
            InferenceErrorCode.PROVIDER_UNAVAILABLE,
            "provider call failed unexpectedly",
            retryable=True,
            details={"error_type": type(exc).__name__},
        )
