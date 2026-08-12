"""Lazy, reusable BLIP image-captioning provider."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from importlib.metadata import PackageNotFoundError, version
from typing import BinaryIO, Protocol

from video_preprocess.domain import (
    ArtifactRef,
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
from video_preprocess.inference.caption import CaptionService
from video_preprocess.inference.gateway import InferenceGateway
from video_preprocess.storage import ArtifactStore
from video_preprocess.storage.errors import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
)

from .fingerprints import resolve_hf_cache_revision


class CaptionProcessor(Protocol):
    """Subset of a transformers processor required by this provider."""

    def __call__(
        self,
        *,
        images: Sequence[object],
        return_tensors: str,
        padding: bool,
    ) -> object: ...

    def batch_decode(
        self,
        sequences: object,
        *,
        skip_special_tokens: bool,
    ) -> Sequence[str]: ...


class CaptionModel(Protocol):
    """Subset of a transformers caption model required by this provider."""

    config: object

    def generate(self, **inputs: object) -> object: ...


ModelLoader = Callable[
    [str, str | None, str | None],
    tuple[CaptionProcessor, CaptionModel],
]
ImageLoader = Callable[[BinaryIO], object]


def _default_loader(
    model_name: str,
    revision: str | None,
    device: str | None,
) -> tuple[CaptionProcessor, CaptionModel]:
    from transformers import BlipForConditionalGeneration, BlipProcessor

    options = {}
    if revision is not None:
        options["revision"] = revision
    processor = BlipProcessor.from_pretrained(model_name, **options)
    model = BlipForConditionalGeneration.from_pretrained(model_name, **options)
    if device is not None:
        model = model.to(device)
    model.eval()
    return processor, model


def _default_image_loader(stream: BinaryIO) -> object:
    from PIL import Image

    with Image.open(stream) as image:
        return image.convert("RGB")


def _runtime_name() -> str:
    try:
        package_version = version("transformers")
    except PackageNotFoundError:
        package_version = "unknown"
    return f"transformers/{package_version}"


class LocalCaptionProvider:
    """In-process caption provider using Artifact Store image inputs."""

    PROVIDER_NAME = "local.caption"
    INPUT_MEDIA_TYPES = ("image/jpeg", "image/png", "image/webp")

    def __init__(
        self,
        *,
        alias: str,
        model_name: str,
        artifact_store: ArtifactStore,
        revision: str | None = None,
        device: str | None = None,
        max_batch_size: int = 16,
        max_artifact_bytes: int = 25 * 1024 * 1024,
        loader: ModelLoader = _default_loader,
        image_loader: ImageLoader = _default_image_loader,
    ) -> None:
        if (
            not isinstance(alias, str)
            or not alias.strip()
            or not isinstance(model_name, str)
            or not model_name.strip()
        ):
            raise ValueError("alias and model_name must be non-empty")
        if (
            isinstance(max_batch_size, bool)
            or not isinstance(max_batch_size, int)
            or max_batch_size < 1
        ):
            raise ValueError("max_batch_size must be at least 1")
        if (
            isinstance(max_artifact_bytes, bool)
            or not isinstance(max_artifact_bytes, int)
            or max_artifact_bytes < 1
        ):
            raise ValueError("max_artifact_bytes must be at least 1")
        if revision is not None and (
            not isinstance(revision, str) or not revision.strip()
        ):
            raise ValueError("revision must be a non-empty string or None")
        if device is not None and (
            not isinstance(device, str) or not device.strip()
        ):
            raise ValueError("device must be a non-empty string or None")
        self.alias = alias
        self.model_name = model_name
        self.artifact_store = artifact_store
        self.revision = revision
        self.requested_revision = revision or "default"
        self.effective_revision = self.requested_revision
        self.device = device
        self.max_batch_size = max_batch_size
        self.max_artifact_bytes = max_artifact_bytes
        self._loader = loader
        self._image_loader = image_loader
        self._processor: CaptionProcessor | None = None
        self._model: CaptionModel | None = None
        self._model_lock = threading.Lock()
        self._inference_lock = threading.Lock()
        self._load_error: str | None = None
        self._responses: dict[str, tuple[str, InferenceResponse]] = {}

    @property
    def is_loaded(self) -> bool:
        return self._processor is not None and self._model is not None

    async def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.PROVIDER_NAME,
            tasks=[InferenceTask.IMAGE_CAPTIONING],
            model_aliases=[self.alias],
            input_media_types=self.INPUT_MEDIA_TYPES,
            features=["artifact_batch", "ordered_captions"],
            max_batch_size=self.max_batch_size,
            max_artifact_bytes=self.max_artifact_bytes,
            supports_cancellation=False,
            supports_async_jobs=False,
        )

    async def health(self) -> ProviderHealth:
        if self._load_error is not None:
            return ProviderHealth(
                provider=self.PROVIDER_NAME,
                status=HealthState.UNAVAILABLE,
                details={"load_error": self._load_error},
            )
        return ProviderHealth(
            provider=self.PROVIDER_NAME,
            status=HealthState.AVAILABLE,
            details={"model_loaded": self.is_loaded},
        )

    async def effective_model(self) -> EffectiveModel | None:
        """Resolve the model this provider would use without loading it."""

        revision = self.effective_revision if self.is_loaded else await (
            asyncio.to_thread(
                resolve_hf_cache_revision,
                self.model_name,
                "config.json",
                self.revision,
            )
        )
        if revision is None:
            return None
        return EffectiveModel(
            provider=self.PROVIDER_NAME,
            name=self.model_name,
            revision=revision,
            runtime=_runtime_name(),
        )

    async def warmup(self) -> None:
        await asyncio.to_thread(self._get_model)

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        return await asyncio.to_thread(self._infer_sync, request)

    async def cancel(self, request_id: str) -> None:
        return None

    def _infer_sync(self, request: InferenceRequest) -> InferenceResponse:
        with self._inference_lock:
            fingerprint = self._fingerprint(request)
            cached = self._responses.get(request.idempotency_key)
            if cached is not None:
                cached_fingerprint, cached_response = cached
                if cached_fingerprint != fingerprint:
                    return self._failure(
                        request,
                        InferenceErrorCode.INVALID_REQUEST,
                        "idempotency key was reused with different input",
                        details={"reason": "IDEMPOTENCY_KEY_CONFLICT"},
                    )
                return self._with_request_id(
                    cached_response,
                    request.request_id,
                )

            validation_error = self._validate_request(request)
            if validation_error is not None:
                return validation_error

            images = request.inputs["images"]
            assert isinstance(images, list)
            loaded_images = []
            for index, artifact in enumerate(images):
                assert isinstance(artifact, ArtifactRef)
                loaded = self._load_image(request, artifact, index)
                if isinstance(loaded, InferenceResponse):
                    return loaded
                loaded_images.append(loaded)

            try:
                processor, model, load_elapsed = self._get_model()
            except Exception as exc:
                self._load_error = type(exc).__name__
                return self._failure(
                    request,
                    InferenceErrorCode.MODEL_UNAVAILABLE,
                    "caption model could not be loaded",
                    details={"error_type": type(exc).__name__},
                )

            inference_start = time.monotonic()
            try:
                max_new_tokens = request.parameters.get("max_new_tokens", 40)
                model_inputs = processor(
                    images=loaded_images,
                    return_tensors="pt",
                    padding=True,
                )
                if self.device is not None and hasattr(model_inputs, "to"):
                    model_inputs = model_inputs.to(self.device)
                if not isinstance(model_inputs, Mapping):
                    raise TypeError("processor output must be a mapping")
                output_ids = model.generate(
                    **model_inputs,
                    max_new_tokens=max_new_tokens,
                )
                raw_captions = processor.batch_decode(
                    output_ids,
                    skip_special_tokens=True,
                )
                captions = self._normalize_captions(
                    raw_captions,
                    expected_count=len(images),
                )
            except Exception as exc:
                return self._failure(
                    request,
                    InferenceErrorCode.INFERENCE_FAILED,
                    "caption model execution failed",
                    details={"error_type": type(exc).__name__},
                )
            inference_elapsed = time.monotonic() - inference_start
            response = InferenceResponse(
                request_id=request.request_id,
                status=InferenceStatus.SUCCEEDED,
                outputs={"captions": captions},
                model=EffectiveModel(
                    provider=self.PROVIDER_NAME,
                    name=self.model_name,
                    revision=self.effective_revision,
                    runtime=_runtime_name(),
                ),
                usage={
                    "input_count": len(images),
                    "batch_size": len(images),
                },
                timing={
                    "model_load_sec": round(load_elapsed, 6),
                    "inference_sec": round(inference_elapsed, 6),
                },
            )
            self._cache_response(request, fingerprint, response)
            return response

    def _load_image(
        self,
        request: InferenceRequest,
        artifact: ArtifactRef,
        index: int,
    ) -> object | InferenceResponse:
        try:
            verification = self.artifact_store.verify(artifact)
            if not verification.exists:
                return self._failure(
                    request,
                    InferenceErrorCode.ARTIFACT_NOT_FOUND,
                    f"caption input artifact is missing: images[{index}]",
                )
            if not verification.ok:
                return self._failure(
                    request,
                    InferenceErrorCode.ARTIFACT_INTEGRITY_ERROR,
                    f"caption input artifact failed verification: images[{index}]",
                )
            with self.artifact_store.open(artifact) as stream:
                return self._image_loader(stream)
        except ArtifactNotFoundError:
            return self._failure(
                request,
                InferenceErrorCode.ARTIFACT_NOT_FOUND,
                f"caption input artifact is missing: images[{index}]",
            )
        except ArtifactIntegrityError:
            return self._failure(
                request,
                InferenceErrorCode.ARTIFACT_INTEGRITY_ERROR,
                f"caption input artifact failed verification: images[{index}]",
            )
        except Exception as exc:
            return self._failure(
                request,
                InferenceErrorCode.INVALID_REQUEST,
                f"caption input image could not be decoded: images[{index}]",
                details={"error_type": type(exc).__name__},
            )

    def _get_model(
        self,
    ) -> tuple[CaptionProcessor, CaptionModel, float]:
        if self._processor is not None and self._model is not None:
            return self._processor, self._model, 0.0
        with self._model_lock:
            if self._processor is not None and self._model is not None:
                return self._processor, self._model, 0.0
            started = time.monotonic()
            processor, model = self._loader(
                self.model_name,
                self.revision,
                self.device,
            )
            self._processor = processor
            self._model = model
            self.effective_revision = self._resolve_revision(
                processor,
                model,
                fallback=self.requested_revision,
            )
            self._load_error = None
            return processor, model, time.monotonic() - started

    def _validate_request(
        self,
        request: InferenceRequest,
    ) -> InferenceResponse | None:
        if request.task is not InferenceTask.IMAGE_CAPTIONING:
            return self._failure(
                request,
                InferenceErrorCode.UNSUPPORTED_CAPABILITY,
                "local caption provider only supports image_captioning",
            )
        if (
            request.model.alias != self.alias
            or request.model.name != self.model_name
            or request.model.revision != self.requested_revision
        ):
            return self._failure(
                request,
                InferenceErrorCode.UNSUPPORTED_CAPABILITY,
                "requested model does not match provider binding",
            )
        images = request.inputs.get("images")
        if not isinstance(images, list) or not images:
            return self._failure(
                request,
                InferenceErrorCode.INVALID_REQUEST,
                "inputs.images must be a non-empty ArtifactRef array",
            )
        if len(images) > self.max_batch_size:
            return self._failure(
                request,
                InferenceErrorCode.INVALID_REQUEST,
                "caption batch exceeds provider maximum",
                details={"max_batch_size": self.max_batch_size},
            )
        for index, image in enumerate(images):
            if not isinstance(image, ArtifactRef):
                return self._failure(
                    request,
                    InferenceErrorCode.INVALID_REQUEST,
                    f"inputs.images[{index}] must be an ArtifactRef",
                )
            if image.media_type not in self.INPUT_MEDIA_TYPES:
                return self._failure(
                    request,
                    InferenceErrorCode.INVALID_REQUEST,
                    f"inputs.images[{index}] has an unsupported media type",
                    details={"media_type": image.media_type},
                )
        max_new_tokens = request.parameters.get("max_new_tokens", 40)
        if (
            isinstance(max_new_tokens, bool)
            or not isinstance(max_new_tokens, int)
            or max_new_tokens < 1
            or max_new_tokens > 512
        ):
            return self._failure(
                request,
                InferenceErrorCode.INVALID_REQUEST,
                "parameters.max_new_tokens must be between 1 and 512",
            )
        return None

    @staticmethod
    def _normalize_captions(
        captions: Sequence[str] | object,
        *,
        expected_count: int,
    ) -> list[str]:
        if (
            isinstance(captions, (str, bytes))
            or not isinstance(captions, Sequence)
            or len(captions) != expected_count
        ):
            raise ValueError("caption output count does not match input")
        normalized = []
        for caption in captions:
            if not isinstance(caption, str) or not caption.strip():
                raise ValueError("caption output contains an empty value")
            normalized.append(caption.strip())
        return normalized

    @staticmethod
    def _resolve_revision(
        processor: CaptionProcessor,
        model: CaptionModel,
        *,
        fallback: str,
    ) -> str:
        candidates = [
            getattr(getattr(model, "config", None), "_commit_hash", None),
            getattr(processor, "_commit_hash", None),
        ]
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                return candidate
        return fallback

    @staticmethod
    def _fingerprint(request: InferenceRequest) -> str:
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

    def _cache_response(
        self,
        request: InferenceRequest,
        fingerprint: str,
        response: InferenceResponse,
    ) -> None:
        if len(self._responses) >= 256:
            self._responses.pop(next(iter(self._responses)))
        self._responses[request.idempotency_key] = (fingerprint, response)

    @staticmethod
    def _with_request_id(
        response: InferenceResponse,
        request_id: str,
    ) -> InferenceResponse:
        error = response.error
        if error is not None:
            error = replace(error, request_id=request_id)
        return replace(response, request_id=request_id, error=error)

    @staticmethod
    def _failure(
        request: InferenceRequest,
        code: InferenceErrorCode,
        message: str,
        *,
        details: dict[str, object] | None = None,
    ) -> InferenceResponse:
        return InferenceResponse(
            request_id=request.request_id,
            status=InferenceStatus.FAILED,
            error=InferenceFailure(
                code=code,
                message=message,
                retryable=False,
                details={} if details is None else details,
                request_id=request.request_id,
            ),
        )


def create_local_caption_service(
    model_name: str,
    artifact_store: ArtifactStore,
    *,
    alias: str = "caption.default",
    revision: str | None = None,
    device: str | None = None,
) -> CaptionService:
    """Create one local service whose provider reuses its loaded model."""

    provider = LocalCaptionProvider(
        alias=alias,
        model_name=model_name,
        artifact_store=artifact_store,
        revision=revision,
        device=device,
    )
    gateway = InferenceGateway({alias: provider})
    return CaptionService(
        gateway,
        alias=alias,
        model_name=model_name,
        revision=provider.requested_revision,
    )
