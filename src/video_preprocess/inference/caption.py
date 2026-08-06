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
    ) -> None:
        self.gateway = gateway
        self.alias = alias
        self.model_name = model_name
        self.revision = revision
        self.timeout_sec = timeout_sec

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
        request_id = f"infer_{uuid.uuid4().hex}"
        trace_id = trace_id or f"trace_{uuid.uuid4().hex}"
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "alias": self.alias,
                    "model": self.model_name,
                    "revision": self.revision,
                    "images": [image.to_dict() for image in normalized_images],
                    "max_new_tokens": max_new_tokens,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        request = InferenceRequest(
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
            inputs={"images": normalized_images},
            parameters={"max_new_tokens": max_new_tokens},
            timeout_sec=self.timeout_sec,
            trace_id=trace_id,
        )
        response = await self.gateway.infer(request)
        if response.status is not InferenceStatus.SUCCEEDED:
            failure = response.error or InferenceFailure(
                code=InferenceErrorCode.INFERENCE_FAILED,
                message="provider failed without an error object",
                retryable=False,
                request_id=request_id,
            )
            raise InferenceCallError(failure)
        if response.model is None:
            raise self._invalid_response(
                request_id,
                "successful caption response has no model",
            )
        captions = self._normalize_captions(
            response.outputs.get("captions"),
            expected_count=len(normalized_images),
            request_id=request_id,
        )
        return CaptionBatch(
            captions=captions,
            model=response.model,
            usage=dict(response.usage),
            timing=dict(response.timing),
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
