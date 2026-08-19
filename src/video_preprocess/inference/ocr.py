"""Task-specific adapter for ordered optical-character recognition."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import uuid
from collections.abc import Mapping, Sequence
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


_LANGUAGE_PATTERN = re.compile(r"^[a-z0-9_]+$")


@dataclass(frozen=True, slots=True)
class OCRRegion:
    """One recognized word and its pixel-space bounding box."""

    region_id: int
    text: str
    confidence: float
    x: int
    y: int
    width: int
    height: int

    def to_dict(self) -> dict[str, object]:
        return {
            "region_id": self.region_id,
            "text": self.text,
            "confidence": self.confidence,
            "bbox": {
                "x": self.x,
                "y": self.y,
                "width": self.width,
                "height": self.height,
            },
        }


@dataclass(frozen=True, slots=True)
class OCRImageResult:
    """Validated OCR data for one input image."""

    artifact_id: str
    text: str
    image_width: int
    image_height: int
    regions: tuple[OCRRegion, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "text": self.text,
            "image_width": self.image_width,
            "image_height": self.image_height,
            "regions": [region.to_dict() for region in self.regions],
        }


@dataclass(frozen=True, slots=True)
class OCRBatch:
    """Ordered OCR results and the effective model that produced them."""

    results: tuple[OCRImageResult, ...]
    model: EffectiveModel
    usage: dict[str, object]
    timing: dict[str, object]


class OCRService:
    """Build OCR requests, chunk by capability, and validate responses."""

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
            or not math.isfinite(float(timeout_sec))
            or timeout_sec <= 0
        ):
            raise ValueError("timeout_sec must be positive and finite")
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

    def recognize(
        self,
        images: Sequence[ArtifactRef],
        *,
        languages: Sequence[str] = ("eng",),
        detect_orientation: bool = True,
        min_confidence: float = 0.5,
        run_id: str = "compat_pipeline",
        stage_run_id: str = "optical_character_recognition",
        trace_id: str | None = None,
    ) -> OCRBatch:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.recognize_async(
                    images,
                    languages=languages,
                    detect_orientation=detect_orientation,
                    min_confidence=min_confidence,
                    run_id=run_id,
                    stage_run_id=stage_run_id,
                    trace_id=trace_id,
                )
            )
        raise RuntimeError(
            "OCRService.recognize() cannot run inside an event loop; "
            "use await recognize_async()"
        )

    async def recognize_async(
        self,
        images: Sequence[ArtifactRef],
        *,
        languages: Sequence[str] = ("eng",),
        detect_orientation: bool = True,
        min_confidence: float = 0.5,
        run_id: str = "compat_pipeline",
        stage_run_id: str = "optical_character_recognition",
        trace_id: str | None = None,
    ) -> OCRBatch:
        normalized_images = self._normalize_images(images)
        normalized_languages = self._normalize_languages(languages)
        if not isinstance(detect_orientation, bool):
            raise ValueError("detect_orientation must be a boolean")
        confidence = self._normalize_confidence(min_confidence)
        trace_id = trace_id or f"trace_{uuid.uuid4().hex}"
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + self.timeout_sec
        capabilities = await self._capabilities(
            deadline,
            request_id=f"infer_{uuid.uuid4().hex}",
        )
        chunk_size = min(
            capabilities.max_batch_size,
            self.batch_size or capabilities.max_batch_size,
        )

        results: list[OCRImageResult] = []
        batch_sizes: list[int] = []
        batch_timing: list[dict[str, object]] = []
        effective_model: EffectiveModel | None = None
        for start in range(0, len(normalized_images), chunk_size):
            image_batch = normalized_images[start:start + chunk_size]
            request_id = f"infer_{uuid.uuid4().hex}"
            request = self._request(
                image_batch,
                request_id=request_id,
                languages=normalized_languages,
                detect_orientation=detect_orientation,
                min_confidence=confidence,
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
                    "successful OCR response has no model",
                )
            if effective_model is None:
                effective_model = response.model
            elif response.model != effective_model:
                raise self._invalid_response(
                    request.request_id,
                    "OCR model metadata changed between chunks",
                )
            results.extend(
                self._normalize_results(
                    response.outputs.get("results"),
                    images=image_batch,
                    request_id=request.request_id,
                )
            )
            batch_sizes.append(len(image_batch))
            batch_timing.append(dict(response.timing))

        assert effective_model is not None
        return OCRBatch(
            results=tuple(results),
            model=effective_model,
            usage={
                "image_count": len(results),
                "region_count": sum(len(result.regions) for result in results),
                "text_char_count": sum(len(result.text) for result in results),
                "batch_size": max(batch_sizes),
                "batch_count": len(batch_sizes),
                "batch_sizes": batch_sizes,
                "configured_batch_size": self.batch_size,
                "provider_max_batch_size": capabilities.max_batch_size,
            },
            timing={
                "total_sec": round(loop.time() - started, 6),
                "inference_sec": self._sum_timing(
                    batch_timing,
                    "inference_sec",
                ),
                "batches": batch_timing,
            },
        )

    def _request(
        self,
        images: list[ArtifactRef],
        *,
        request_id: str,
        languages: tuple[str, ...],
        detect_orientation: bool,
        min_confidence: float,
        run_id: str,
        stage_run_id: str,
        trace_id: str,
        timeout_sec: float,
    ) -> InferenceRequest:
        semantics = {
            "alias": self.alias,
            "model": self.model_name,
            "revision": self.revision,
            "images": [image.to_dict() for image in images],
            "languages": list(languages),
            "detect_orientation": detect_orientation,
            "min_confidence": min_confidence,
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                semantics,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return InferenceRequest(
            request_id=request_id,
            idempotency_key=f"ocr_{fingerprint}",
            run_id=run_id,
            stage_run_id=stage_run_id,
            task=InferenceTask.OPTICAL_CHARACTER_RECOGNITION,
            model=RequestedModel(
                alias=self.alias,
                name=self.model_name,
                revision=self.revision,
            ),
            inputs={"images": images},
            parameters={
                "languages": list(languages),
                "detect_orientation": detect_orientation,
                "min_confidence": min_confidence,
            },
            timeout_sec=timeout_sec,
            trace_id=trace_id,
        )

    async def _capabilities(
        self,
        deadline: float,
        *,
        request_id: str,
    ) -> ProviderCapabilities:
        try:
            capabilities = await asyncio.wait_for(
                self.gateway.capabilities(self.alias),
                timeout=self._remaining_timeout(
                    deadline,
                    request_id=request_id,
                ),
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
            or InferenceTask.OPTICAL_CHARACTER_RECOGNITION
            not in capabilities.tasks
        ):
            raise self._call_error(
                request_id,
                InferenceErrorCode.UNSUPPORTED_CAPABILITY,
                "provider does not support the requested OCR model",
            )
        return capabilities

    @staticmethod
    def _remaining_timeout(deadline: float, *, request_id: str) -> float:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise OCRService._call_error(
                request_id,
                InferenceErrorCode.PROVIDER_TIMEOUT,
                "OCR inference deadline elapsed between batches",
                retryable=True,
            )
        return remaining

    @staticmethod
    def _sum_timing(
        batches: list[dict[str, object]],
        field_name: str,
    ) -> float:
        return round(
            sum(
                float(batch[field_name])
                for batch in batches
                if isinstance(batch.get(field_name), (int, float))
                and not isinstance(batch[field_name], bool)
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
    def _normalize_languages(languages: Sequence[str]) -> tuple[str, ...]:
        if isinstance(languages, (str, bytes)) or not isinstance(
            languages,
            Sequence,
        ):
            raise ValueError("languages must be a sequence of language IDs")
        normalized = []
        for index, language in enumerate(languages):
            if not isinstance(language, str) or not language.strip():
                raise ValueError(f"languages[{index}] must be non-empty")
            language_id = language.strip().lower()
            if not _LANGUAGE_PATTERN.fullmatch(language_id):
                raise ValueError(
                    f"languages[{index}] must contain lowercase letters, "
                    "digits, or underscore"
                )
            if language_id not in normalized:
                normalized.append(language_id)
        if not normalized:
            raise ValueError("languages must not be empty")
        return tuple(normalized)

    @staticmethod
    def _normalize_confidence(value: float) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0 <= float(value) <= 1
        ):
            raise ValueError("min_confidence must be between 0 and 1")
        return float(value)

    @classmethod
    def _normalize_results(
        cls,
        value: object,
        *,
        images: list[ArtifactRef],
        request_id: str,
    ) -> tuple[OCRImageResult, ...]:
        if not isinstance(value, list) or len(value) != len(images):
            raise cls._invalid_response(
                request_id,
                "OCR response count does not match input images",
            )
        return tuple(
            cls._normalize_result(
                item,
                image=image,
                request_id=request_id,
                result_index=index,
            )
            for index, (item, image) in enumerate(zip(value, images))
        )

    @classmethod
    def _normalize_result(
        cls,
        value: object,
        *,
        image: ArtifactRef,
        request_id: str,
        result_index: int,
    ) -> OCRImageResult:
        if not isinstance(value, Mapping):
            raise cls._invalid_response(
                request_id,
                f"OCR result {result_index} must be an object",
            )
        if value.get("artifact_id") != image.artifact_id:
            raise cls._invalid_response(
                request_id,
                f"OCR result {result_index} artifact_id is out of order",
            )
        text = value.get("text")
        if not isinstance(text, str):
            raise cls._invalid_response(
                request_id,
                f"OCR result {result_index} text must be a string",
            )
        image_width = cls._positive_integer(
            value.get("image_width"),
            request_id=request_id,
            field_name=f"OCR result {result_index} image_width",
        )
        image_height = cls._positive_integer(
            value.get("image_height"),
            request_id=request_id,
            field_name=f"OCR result {result_index} image_height",
        )
        raw_regions = value.get("regions")
        if not isinstance(raw_regions, list):
            raise cls._invalid_response(
                request_id,
                f"OCR result {result_index} regions must be an array",
            )
        regions = tuple(
            cls._normalize_region(
                region,
                expected_id=region_index,
                image_width=image_width,
                image_height=image_height,
                request_id=request_id,
                result_index=result_index,
            )
            for region_index, region in enumerate(raw_regions, start=1)
        )
        return OCRImageResult(
            artifact_id=image.artifact_id,
            text=text.strip(),
            image_width=image_width,
            image_height=image_height,
            regions=regions,
        )

    @classmethod
    def _normalize_region(
        cls,
        value: object,
        *,
        expected_id: int,
        image_width: int,
        image_height: int,
        request_id: str,
        result_index: int,
    ) -> OCRRegion:
        if not isinstance(value, Mapping):
            raise cls._invalid_response(
                request_id,
                f"OCR result {result_index} region must be an object",
            )
        if value.get("region_id") != expected_id:
            raise cls._invalid_response(
                request_id,
                f"OCR result {result_index} region IDs must be consecutive",
            )
        text = value.get("text")
        if not isinstance(text, str) or not text.strip():
            raise cls._invalid_response(
                request_id,
                f"OCR result {result_index} region text must be non-empty",
            )
        confidence = value.get("confidence")
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(float(confidence))
            or not 0 <= float(confidence) <= 1
        ):
            raise cls._invalid_response(
                request_id,
                f"OCR result {result_index} confidence must be 0..1",
            )
        bbox = value.get("bbox")
        if not isinstance(bbox, Mapping):
            raise cls._invalid_response(
                request_id,
                f"OCR result {result_index} bbox must be an object",
            )
        coordinates = {}
        for field_name in ("x", "y", "width", "height"):
            coordinate = bbox.get(field_name)
            if (
                isinstance(coordinate, bool)
                or not isinstance(coordinate, int)
                or coordinate < 0
            ):
                raise cls._invalid_response(
                    request_id,
                    f"OCR result {result_index} bbox.{field_name} is invalid",
                )
            coordinates[field_name] = coordinate
        if coordinates["width"] < 1 or coordinates["height"] < 1:
            raise cls._invalid_response(
                request_id,
                f"OCR result {result_index} bbox size must be positive",
            )
        if (
            coordinates["x"] + coordinates["width"] > image_width
            or coordinates["y"] + coordinates["height"] > image_height
        ):
            raise cls._invalid_response(
                request_id,
                f"OCR result {result_index} bbox exceeds image bounds",
            )
        return OCRRegion(
            region_id=expected_id,
            text=text.strip(),
            confidence=round(float(confidence), 6),
            x=coordinates["x"],
            y=coordinates["y"],
            width=coordinates["width"],
            height=coordinates["height"],
        )

    @classmethod
    def _positive_integer(
        cls,
        value: object,
        *,
        request_id: str,
        field_name: str,
    ) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise cls._invalid_response(
                request_id,
                f"{field_name} must be a positive integer",
            )
        return value

    @staticmethod
    def _invalid_response(
        request_id: str,
        message: str,
    ) -> InferenceCallError:
        return OCRService._call_error(
            request_id,
            InferenceErrorCode.INFERENCE_FAILED,
            message,
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
