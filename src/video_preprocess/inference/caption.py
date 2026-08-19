"""Task-specific adapter for image-captioning inference responses."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from video_preprocess.domain import (
    ArtifactRef,
    EffectiveModel,
    InferenceErrorCode,
    InferenceFailure,
    InferenceRequest,
    InferenceStatus,
    InferenceTask,
    ProviderCapabilities,
    RequestedModel,
)

from .errors import InferenceCallError
from .gateway import InferenceGateway


@dataclass(frozen=True, slots=True)
class CaptionBatch:
    """Validated captions returned in the same order as input images."""

    captions: tuple[str, ...]
    model: EffectiveModel
    usage: dict[str, object]
    timing: dict[str, object]


class CaptionService:
    """Build image-captioning requests and validate provider outputs."""

    def __init__(
        self,
        gateway: InferenceGateway,
        *,
        alias: str,
        model_name: str,
        revision: str,
        timeout_sec: float = 300.0,
        batch_size: int | None = None,
    ) -> None:
        if (
            isinstance(timeout_sec, bool)
            or not isinstance(timeout_sec, (int, float))
            or timeout_sec <= 0
        ):
            raise ValueError("timeout_sec must be positive")
        if batch_size is not None and (
            isinstance(batch_size, bool)
            or not isinstance(batch_size, int)
            or batch_size < 1
        ):
            raise ValueError("batch_size must be at least 1 or None")
        self.gateway = gateway
        self.alias = alias
        self.model_name = model_name
        self.revision = revision
        self.timeout_sec = float(timeout_sec)
        self.batch_size = batch_size

    def caption(
        self,
        images: Sequence[ArtifactRef],
        *,
        max_new_tokens: int = 40,
        run_id: str = "compat_pipeline",
        stage_run_id: str = "image_captioning",
        trace_id: str | None = None,
    ) -> CaptionBatch:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.caption_async(
                    images,
                    max_new_tokens=max_new_tokens,
                    run_id=run_id,
                    stage_run_id=stage_run_id,
                    trace_id=trace_id,
                )
            )
        raise RuntimeError(
            "CaptionService.caption() cannot run inside an event loop; "
            "use await caption_async()"
        )

    async def caption_async(
        self,
        images: Sequence[ArtifactRef],
        *,
        max_new_tokens: int = 40,
        run_id: str = "compat_pipeline",
        stage_run_id: str = "image_captioning",
        trace_id: str | None = None,
    ) -> CaptionBatch:
        normalized_images = self._normalize_images(images)
        max_new_tokens = self._normalize_max_new_tokens(max_new_tokens)
        trace_id = trace_id or f"trace_{uuid.uuid4().hex}"
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + self.timeout_sec
        capabilities = await self._capabilities(
            deadline,
            request_id=f"infer_{uuid.uuid4().hex}",
        )
        batch_size = min(
            capabilities.max_batch_size,
            self.batch_size or capabilities.max_batch_size,
        )

        captions: list[str] = []
        batch_sizes: list[int] = []
        batch_usage: list[dict[str, object]] = []
        batch_timing: list[dict[str, object]] = []
        effective_model: EffectiveModel | None = None
        for start in range(0, len(normalized_images), batch_size):
            image_batch = normalized_images[start:start + batch_size]
            request_id = f"infer_{uuid.uuid4().hex}"
            request = self._request(
                image_batch,
                request_id=request_id,
                max_new_tokens=max_new_tokens,
                run_id=run_id,
                stage_run_id=stage_run_id,
                trace_id=trace_id,
                timeout_sec=self._remaining_timeout(
                    deadline,
                    request_id=request_id,
                ),
            )
            response = await self.gateway.infer(request)
            if response.status is not InferenceStatus.SUCCEEDED:
                failure = response.error or InferenceFailure(
                    code=InferenceErrorCode.INFERENCE_FAILED,
                    message="provider failed without an error object",
                    retryable=False,
                    request_id=request.request_id,
                )
                raise InferenceCallError(failure)
            if response.model is None:
                raise self._invalid_response(
                    request.request_id,
                    "successful caption response has no model",
                )
            if effective_model is None:
                effective_model = response.model
            elif response.model != effective_model:
                raise self._invalid_response(
                    request.request_id,
                    "caption batch model metadata changed between chunks",
                )
            captions.extend(
                self._normalize_captions(
                    response.outputs.get("captions"),
                    expected_count=len(image_batch),
                    request_id=request.request_id,
                )
            )
            batch_sizes.append(len(image_batch))
            batch_usage.append(dict(response.usage))
            batch_timing.append(dict(response.timing))

        assert effective_model is not None
        usage = {
            "input_count": len(normalized_images),
            "batch_size": max(batch_sizes),
            "batch_count": len(batch_sizes),
            "batch_sizes": batch_sizes,
            "configured_batch_size": self.batch_size,
            "provider_max_batch_size": capabilities.max_batch_size,
        }
        devices = {
            item["device"]
            for item in batch_usage
            if isinstance(item.get("device"), str)
        }
        if len(devices) == 1:
            usage["device"] = devices.pop()
        timing = {
            "total_sec": round(loop.time() - started, 6),
            "model_load_sec": self._sum_timing(
                batch_timing,
                "model_load_sec",
            ),
            "inference_sec": self._sum_timing(
                batch_timing,
                "inference_sec",
            ),
            "batches": batch_timing,
        }
        return CaptionBatch(
            captions=tuple(captions),
            model=effective_model,
            usage=usage,
            timing=timing,
        )

    def _request(
        self,
        images: list[ArtifactRef],
        *,
        request_id: str,
        max_new_tokens: int,
        run_id: str,
        stage_run_id: str,
        trace_id: str,
        timeout_sec: float,
    ) -> InferenceRequest:
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "alias": self.alias,
                    "model": self.model_name,
                    "revision": self.revision,
                    "images": [image.to_dict() for image in images],
                    "max_new_tokens": max_new_tokens,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return InferenceRequest(
            request_id=request_id,
            idempotency_key=f"caption_{fingerprint}",
            run_id=run_id,
            stage_run_id=stage_run_id,
            task=InferenceTask.IMAGE_CAPTIONING,
            model=RequestedModel(
                alias=self.alias,
                name=self.model_name,
                revision=self.revision,
            ),
            inputs={"images": images},
            parameters={"max_new_tokens": max_new_tokens},
            timeout_sec=timeout_sec,
            trace_id=trace_id,
        )

    async def _capabilities(
        self,
        deadline: float,
        *,
        request_id: str,
    ) -> ProviderCapabilities:
        timeout_sec = self._remaining_timeout(
            deadline,
            request_id=request_id,
        )
        try:
            capabilities = await asyncio.wait_for(
                self.gateway.capabilities(self.alias),
                timeout=timeout_sec,
            )
        except asyncio.TimeoutError as exc:
            raise self._call_error(
                request_id,
                InferenceErrorCode.PROVIDER_TIMEOUT,
                "provider capability check timed out",
                retryable=True,
            ) from exc
        except InferenceCallError:
            raise
        except Exception as exc:
            raise self._call_error(
                request_id,
                InferenceErrorCode.PROVIDER_UNAVAILABLE,
                "provider capability check failed",
                details={"error_type": type(exc).__name__},
            ) from exc
        if not isinstance(capabilities, ProviderCapabilities):
            raise self._call_error(
                request_id,
                InferenceErrorCode.PROVIDER_UNAVAILABLE,
                "provider returned invalid capabilities",
            )
        if (
            self.alias not in capabilities.model_aliases
            or InferenceTask.IMAGE_CAPTIONING not in capabilities.tasks
        ):
            raise self._call_error(
                request_id,
                InferenceErrorCode.UNSUPPORTED_CAPABILITY,
                "provider does not support the requested caption model",
            )
        return capabilities

    @staticmethod
    def _remaining_timeout(deadline: float, *, request_id: str) -> float:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise CaptionService._call_error(
                request_id,
                InferenceErrorCode.PROVIDER_TIMEOUT,
                "caption inference deadline elapsed between batches",
                retryable=True,
            )
        return remaining

    @staticmethod
    def _sum_timing(
        batch_timing: list[dict[str, object]],
        field_name: str,
    ) -> float:
        return round(
            sum(
                float(item[field_name])
                for item in batch_timing
                if isinstance(item.get(field_name), (int, float))
                and not isinstance(item[field_name], bool)
            ),
            6,
        )

    @staticmethod
    def _normalize_images(
        images: Sequence[ArtifactRef],
    ) -> list[ArtifactRef]:
        if isinstance(images, (str, bytes)) or not isinstance(images, Sequence):
            raise ValueError("images must be a sequence of ArtifactRef values")
        normalized = list(images)
        if not normalized:
            raise ValueError("images must not be empty")
        for index, image in enumerate(normalized):
            if not isinstance(image, ArtifactRef):
                raise ValueError(f"images[{index}] must be an ArtifactRef")
        return normalized

    @staticmethod
    def _normalize_max_new_tokens(value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("max_new_tokens must be an integer")
        if value < 1 or value > 512:
            raise ValueError("max_new_tokens must be between 1 and 512")
        return value

    @classmethod
    def _normalize_captions(
        cls,
        value: object,
        *,
        expected_count: int,
        request_id: str,
    ) -> tuple[str, ...]:
        if not isinstance(value, list) or len(value) != expected_count:
            raise cls._invalid_response(
                request_id,
                "caption response count does not match input images",
            )
        captions = []
        for index, caption in enumerate(value):
            if not isinstance(caption, str) or not caption.strip():
                raise cls._invalid_response(
                    request_id,
                    f"caption {index} must be a non-empty string",
                )
            captions.append(caption.strip())
        return tuple(captions)

    @staticmethod
    def _invalid_response(
        request_id: str,
        message: str,
    ) -> InferenceCallError:
        return InferenceCallError(
            InferenceFailure(
                code=InferenceErrorCode.INFERENCE_FAILED,
                message=message,
                retryable=False,
                request_id=request_id,
            )
        )

    @staticmethod
    def _call_error(
        request_id: str,
        code: InferenceErrorCode,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, object] | None = None,
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
