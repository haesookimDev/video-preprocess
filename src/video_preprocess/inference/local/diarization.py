"""Lazy, reusable pyannote speaker diarization provider."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Protocol

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
from video_preprocess.inference.diarization import DiarizationService
from video_preprocess.inference.gateway import InferenceGateway
from video_preprocess.storage import ArtifactStore
from video_preprocess.storage.errors import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
)

from .fingerprints import resolve_hf_cache_revision


class DiarizationPipeline(Protocol):
    """Subset of a pyannote pipeline required by this provider."""

    def __call__(self, file: str) -> object: ...


ModelLoader = Callable[
    [str, str | None, str, str | None],
    tuple[DiarizationPipeline, str],
]


def _snapshot_revision(path: str, fallback: str) -> str:
    # Hub cache files are symlinks into ``blobs``. Keep the lexical snapshot
    # path so resolving the symlink does not discard the commit directory.
    parts = Path(path).parts
    for index, part in enumerate(parts[:-1]):
        if part == "snapshots":
            candidate = parts[index + 1]
            if candidate:
                return candidate
    return fallback


def _default_loader(
    model_name: str,
    revision: str | None,
    token: str,
    device: str | None,
) -> tuple[DiarizationPipeline, str]:
    from huggingface_hub import hf_hub_download
    from pyannote.audio import Pipeline

    requested_revision = revision or "default"
    local_checkpoint = Path(model_name).exists()
    if local_checkpoint:
        effective_revision = requested_revision
    else:
        config_path = hf_hub_download(
            repo_id=model_name,
            filename="config.yaml",
            revision=revision,
            token=token,
        )
        effective_revision = _snapshot_revision(
            config_path,
            requested_revision,
        )
    pipeline = Pipeline.from_pretrained(
        model_name,
        revision=None if local_checkpoint else revision,
        token=token,
    )
    if pipeline is None:
        raise RuntimeError("pyannote returned no pipeline")
    if device is not None:
        import torch

        pipeline.to(torch.device(device))
    return pipeline, effective_revision


def _runtime_name() -> str:
    try:
        pyannote_version = version("pyannote.audio")
    except PackageNotFoundError:
        pyannote_version = "unknown"
    try:
        torch_version = version("torch")
    except PackageNotFoundError:
        torch_version = "unknown"
    return f"pyannote.audio/{pyannote_version};torch/{torch_version}"


def _finite_number(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be finite")
    return number


def _load_failure_code(exc: Exception) -> InferenceErrorCode:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code == 401:
        return InferenceErrorCode.AUTHENTICATION_FAILED
    if status_code == 403 or type(exc).__name__ == "GatedRepoError":
        return InferenceErrorCode.MODEL_ACCESS_DENIED
    return InferenceErrorCode.MODEL_UNAVAILABLE


class LocalDiarizationProvider:
    """In-process pyannote provider using one audio ArtifactRef."""

    PROVIDER_NAME = "local.diarization"

    def __init__(
        self,
        *,
        alias: str,
        model_name: str,
        artifact_store: ArtifactStore,
        token: str | None,
        revision: str | None = None,
        device: str | None = None,
        max_artifact_bytes: int = 4 * 1024 * 1024 * 1024,
        loader: ModelLoader = _default_loader,
    ) -> None:
        if (
            not isinstance(alias, str)
            or not alias.strip()
            or not isinstance(model_name, str)
            or not model_name.strip()
        ):
            raise ValueError("alias and model_name must be non-empty")
        if token is not None and (
            not isinstance(token, str) or not token.strip()
        ):
            raise ValueError("token must be a non-empty string or None")
        if revision is not None and (
            not isinstance(revision, str) or not revision.strip()
        ):
            raise ValueError("revision must be a non-empty string or None")
        if device is not None and (
            not isinstance(device, str) or not device.strip()
        ):
            raise ValueError("device must be a non-empty string or None")
        if (
            isinstance(max_artifact_bytes, bool)
            or not isinstance(max_artifact_bytes, int)
            or max_artifact_bytes < 1
        ):
            raise ValueError("max_artifact_bytes must be at least 1")
        self.alias = alias
        self.model_name = model_name
        self.artifact_store = artifact_store
        self.revision = revision
        self.requested_revision = revision or "default"
        self.effective_revision = self.requested_revision
        self.device = device
        self.max_artifact_bytes = max_artifact_bytes
        self._token = token.strip() if token is not None else None
        self._loader = loader
        self._pipeline: DiarizationPipeline | None = None
        self._model_lock = threading.Lock()
        self._inference_lock = threading.Lock()
        self._load_error: str | None = None
        self._responses: dict[str, tuple[str, InferenceResponse]] = {}

    @property
    def is_loaded(self) -> bool:
        return self._pipeline is not None

    async def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.PROVIDER_NAME,
            tasks=[InferenceTask.SPEAKER_DIARIZATION],
            model_aliases=[self.alias],
            input_media_types=["audio/wav"],
            features=["speaker_turns", "overlapping_speech"],
            max_batch_size=1,
            max_artifact_bytes=self.max_artifact_bytes,
            supports_cancellation=False,
            supports_async_jobs=False,
        )

    async def health(self) -> ProviderHealth:
        if self._token is None:
            return ProviderHealth(
                provider=self.PROVIDER_NAME,
                status=HealthState.UNAVAILABLE,
                details={"credential": "missing"},
            )
        if self._load_error is not None:
            return ProviderHealth(
                provider=self.PROVIDER_NAME,
                status=HealthState.UNAVAILABLE,
                details={"load_error": self._load_error},
            )
        return ProviderHealth(
            provider=self.PROVIDER_NAME,
            status=HealthState.AVAILABLE,
            details={
                "model_loaded": self.is_loaded,
                "device": self.device or "model_default",
            },
        )

    async def effective_model(self) -> EffectiveModel | None:
        """Resolve the accessible model without loading the pipeline."""

        if self._token is None:
            return None
        revision = self.effective_revision if self.is_loaded else await (
            asyncio.to_thread(
                resolve_hf_cache_revision,
                self.model_name,
                "config.yaml",
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
        await asyncio.to_thread(self._get_pipeline)

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
            if self._token is None:
                return self._failure(
                    request,
                    InferenceErrorCode.AUTHENTICATION_FAILED,
                    "HF_TOKEN is required for local diarization",
                    details={"reason": "CREDENTIAL_MISSING"},
                )

            audio_ref = request.inputs["audio"]
            assert isinstance(audio_ref, ArtifactRef)
            verification_error = self._verify_audio(request, audio_ref)
            if verification_error is not None:
                return verification_error

            try:
                pipeline, load_elapsed = self._get_pipeline()
            except Exception as exc:
                self._load_error = type(exc).__name__
                code = _load_failure_code(exc)
                messages = {
                    InferenceErrorCode.AUTHENTICATION_FAILED: (
                        "HF credential was rejected"
                    ),
                    InferenceErrorCode.MODEL_ACCESS_DENIED: (
                        "diarization model access was denied"
                    ),
                    InferenceErrorCode.MODEL_UNAVAILABLE: (
                        "diarization model could not be loaded"
                    ),
                }
                return self._failure(
                    request,
                    code,
                    messages[code],
                    details={"error_type": type(exc).__name__},
                )

            inference_start = time.monotonic()
            try:
                with tempfile.TemporaryDirectory(
                    prefix="video-preprocess-diarization-"
                ) as workspace:
                    audio_path = self.artifact_store.materialize(
                        audio_ref,
                        Path(workspace),
                    )
                    raw_result = pipeline(str(audio_path))
                speakers, turns = self._normalize_result(raw_result)
            except ArtifactNotFoundError:
                return self._failure(
                    request,
                    InferenceErrorCode.ARTIFACT_NOT_FOUND,
                    "diarization input audio artifact is missing",
                )
            except ArtifactIntegrityError:
                return self._failure(
                    request,
                    InferenceErrorCode.ARTIFACT_INTEGRITY_ERROR,
                    "diarization input audio artifact failed verification",
                )
            except Exception as exc:
                return self._failure(
                    request,
                    InferenceErrorCode.INFERENCE_FAILED,
                    "diarization model execution failed",
                    details={"error_type": type(exc).__name__},
                )
            inference_elapsed = time.monotonic() - inference_start
            response = InferenceResponse(
                request_id=request.request_id,
                status=InferenceStatus.SUCCEEDED,
                outputs={"speakers": speakers, "turns": turns},
                model=EffectiveModel(
                    provider=self.PROVIDER_NAME,
                    name=self.model_name,
                    revision=self.effective_revision,
                    runtime=_runtime_name(),
                ),
                usage={
                    "speaker_count": len(speakers),
                    "turn_count": len(turns),
                },
                timing={
                    "model_load_sec": round(load_elapsed, 6),
                    "inference_sec": round(inference_elapsed, 6),
                },
            )
            self._cache_response(request, fingerprint, response)
            return response

    def _verify_audio(
        self,
        request: InferenceRequest,
        artifact: ArtifactRef,
    ) -> InferenceResponse | None:
        try:
            verification = self.artifact_store.verify(artifact)
            if not verification.exists:
                return self._failure(
                    request,
                    InferenceErrorCode.ARTIFACT_NOT_FOUND,
                    "diarization input audio artifact is missing",
                )
            if not verification.ok:
                return self._failure(
                    request,
                    InferenceErrorCode.ARTIFACT_INTEGRITY_ERROR,
                    "diarization input audio artifact failed verification",
                )
        except ArtifactNotFoundError:
            return self._failure(
                request,
                InferenceErrorCode.ARTIFACT_NOT_FOUND,
                "diarization input audio artifact is missing",
            )
        except ArtifactIntegrityError:
            return self._failure(
                request,
                InferenceErrorCode.ARTIFACT_INTEGRITY_ERROR,
                "diarization input audio artifact failed verification",
            )
        return None

    def _get_pipeline(
        self,
    ) -> tuple[DiarizationPipeline, float]:
        if self._token is None:
            raise RuntimeError("HF_TOKEN is missing")
        if self._pipeline is not None:
            return self._pipeline, 0.0
        with self._model_lock:
            if self._pipeline is not None:
                return self._pipeline, 0.0
            started = time.monotonic()
            pipeline, effective_revision = self._loader(
                self.model_name,
                self.revision,
                self._token,
                self.device,
            )
            self._pipeline = pipeline
            self.effective_revision = effective_revision
            self._load_error = None
            return pipeline, time.monotonic() - started

    def _validate_request(
        self,
        request: InferenceRequest,
    ) -> InferenceResponse | None:
        if request.task is not InferenceTask.SPEAKER_DIARIZATION:
            return self._failure(
                request,
                InferenceErrorCode.UNSUPPORTED_CAPABILITY,
                "local diarization provider only supports speaker_diarization",
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
        audio = request.inputs.get("audio")
        if not isinstance(audio, ArtifactRef):
            return self._failure(
                request,
                InferenceErrorCode.INVALID_REQUEST,
                "inputs.audio must be an ArtifactRef",
            )
        if audio.media_type != "audio/wav":
            return self._failure(
                request,
                InferenceErrorCode.INVALID_REQUEST,
                "inputs.audio must use media type audio/wav",
                details={"media_type": audio.media_type},
            )
        if request.parameters:
            return self._failure(
                request,
                InferenceErrorCode.INVALID_REQUEST,
                "speaker diarization does not accept parameters",
            )
        return None

    @staticmethod
    def _normalize_result(
        result: object,
    ) -> tuple[list[str], list[dict[str, object]]]:
        annotation = getattr(result, "speaker_diarization", result)
        itertracks = getattr(annotation, "itertracks", None)
        if not callable(itertracks):
            raise ValueError("diarization result has no speaker annotation")
        turns = []
        previous_start = 0.0
        for index, item in enumerate(
            itertracks(yield_label=True),
            start=1,
        ):
            try:
                segment, _, speaker = item
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid diarization track") from exc
            start = _finite_number(getattr(segment, "start", None), "start")
            end = _finite_number(getattr(segment, "end", None), "end")
            if start < 0 or end <= start:
                raise ValueError("diarization timestamps are invalid")
            if index > 1 and start < previous_start:
                raise ValueError("diarization turns are not ordered")
            if not isinstance(speaker, str) or not speaker.strip():
                raise ValueError("diarization speaker label is invalid")
            turns.append(
                {
                    "turn_id": index,
                    "start_sec": round(start, 3),
                    "end_sec": round(end, 3),
                    "speaker": speaker.strip(),
                }
            )
            previous_start = start
        speakers = sorted({turn["speaker"] for turn in turns})
        return speakers, turns

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


def create_local_diarization_service(
    model_name: str,
    artifact_store: ArtifactStore,
    *,
    token: str | None,
    alias: str = "diarization.default",
    revision: str | None = None,
    device: str | None = None,
) -> DiarizationService:
    """Create one local service whose pyannote pipeline is reused."""

    provider = LocalDiarizationProvider(
        alias=alias,
        model_name=model_name,
        artifact_store=artifact_store,
        token=token,
        revision=revision,
        device=device,
    )
    gateway = InferenceGateway({alias: provider})
    return DiarizationService(
        gateway,
        alias=alias,
        model_name=model_name,
        revision=provider.requested_revision,
    )
