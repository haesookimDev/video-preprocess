"""Lazy, reusable faster-whisper Silero VAD provider."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
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
from video_preprocess.inference.gateway import InferenceGateway
from video_preprocess.inference.vad import VADService
from video_preprocess.storage import ArtifactStore
from video_preprocess.storage.errors import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
)

from .fingerprints import resolve_vad_asset_revision


class AudioBuffer(Protocol):
    """Decoded mono audio buffer used by Silero VAD."""

    def __len__(self) -> int: ...


class VADBackend(Protocol):
    """Audio decoder and Silero detector bound to one model instance."""

    def decode(self, stream: BinaryIO, sampling_rate: int) -> AudioBuffer: ...

    def detect(
        self,
        audio: AudioBuffer,
        *,
        min_silence_duration_ms: int,
        speech_pad_ms: int,
        sampling_rate: int,
    ) -> Sequence[Mapping[str, object]]: ...


BackendLoader = Callable[[], tuple[VADBackend, str]]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _default_loader() -> tuple[VADBackend, str]:
    from faster_whisper.audio import decode_audio
    from faster_whisper.utils import get_assets_path
    from faster_whisper.vad import (
        VadOptions,
        get_speech_timestamps,
        get_vad_model,
    )

    get_vad_model()
    asset_path = Path(get_assets_path()) / "silero_vad_v6.onnx"

    class FasterWhisperVADBackend:
        def decode(
            self,
            stream: BinaryIO,
            sampling_rate: int,
        ) -> AudioBuffer:
            return decode_audio(stream, sampling_rate=sampling_rate)

        def detect(
            self,
            audio: AudioBuffer,
            *,
            min_silence_duration_ms: int,
            speech_pad_ms: int,
            sampling_rate: int,
        ) -> Sequence[Mapping[str, object]]:
            options = VadOptions(
                min_silence_duration_ms=min_silence_duration_ms,
                speech_pad_ms=speech_pad_ms,
            )
            return get_speech_timestamps(
                audio,
                options,
                sampling_rate=sampling_rate,
            )

    return (
        FasterWhisperVADBackend(),
        f"sha256:{_sha256_file(asset_path)}",
    )


def _runtime_name() -> str:
    try:
        whisper_version = version("faster-whisper")
    except PackageNotFoundError:
        whisper_version = "unknown"
    try:
        onnx_version = version("onnxruntime")
    except PackageNotFoundError:
        onnx_version = "unknown"
    return f"faster-whisper/{whisper_version};onnxruntime/{onnx_version}"


def _integer_sample(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    return value


class LocalVADProvider:
    """In-process Silero VAD provider using one audio ArtifactRef."""

    PROVIDER_NAME = "local.vad"
    SAMPLE_RATE = 16000

    def __init__(
        self,
        *,
        alias: str,
        model_name: str,
        artifact_store: ArtifactStore,
        revision: str | None = None,
        max_artifact_bytes: int = 4 * 1024 * 1024 * 1024,
        loader: BackendLoader = _default_loader,
    ) -> None:
        if (
            not isinstance(alias, str)
            or not alias.strip()
            or not isinstance(model_name, str)
            or not model_name.strip()
        ):
            raise ValueError("alias and model_name must be non-empty")
        if revision is not None and (
            not isinstance(revision, str) or not revision.strip()
        ):
            raise ValueError("revision must be a non-empty string or None")
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
        self.max_artifact_bytes = max_artifact_bytes
        self._loader = loader
        self._backend: VADBackend | None = None
        self._model_lock = threading.Lock()
        self._inference_lock = threading.Lock()
        self._load_error: str | None = None
        self._responses: dict[str, tuple[str, InferenceResponse]] = {}

    @property
    def is_loaded(self) -> bool:
        return self._backend is not None

    async def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.PROVIDER_NAME,
            tasks=[InferenceTask.VOICE_ACTIVITY_DETECTION],
            model_aliases=[self.alias],
            input_media_types=["audio/wav"],
            features=[
                "min_silence_duration_ms",
                "speech_pad_ms",
                "speech_segments",
            ],
            max_batch_size=1,
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
        """Resolve the packaged ONNX fingerprint without loading it."""

        revision = self.effective_revision if self.is_loaded else await (
            asyncio.to_thread(resolve_vad_asset_revision)
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
        await asyncio.to_thread(self._get_backend)

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
            audio_ref = request.inputs["audio"]
            assert isinstance(audio_ref, ArtifactRef)
            verification_error = self._verify_audio(request, audio_ref)
            if verification_error is not None:
                return verification_error

            try:
                backend, load_elapsed = self._get_backend()
            except Exception as exc:
                self._load_error = type(exc).__name__
                return self._failure(
                    request,
                    InferenceErrorCode.MODEL_UNAVAILABLE,
                    "VAD model could not be loaded",
                    details={"error_type": type(exc).__name__},
                )

            decode_start = time.monotonic()
            try:
                with self.artifact_store.open(audio_ref) as stream:
                    audio = backend.decode(stream, self.SAMPLE_RATE)
                sample_count = len(audio)
            except ArtifactNotFoundError:
                return self._failure(
                    request,
                    InferenceErrorCode.ARTIFACT_NOT_FOUND,
                    "VAD input audio artifact is missing",
                )
            except ArtifactIntegrityError:
                return self._failure(
                    request,
                    InferenceErrorCode.ARTIFACT_INTEGRITY_ERROR,
                    "VAD input audio artifact failed verification",
                )
            except Exception as exc:
                return self._failure(
                    request,
                    InferenceErrorCode.INVALID_REQUEST,
                    "VAD input audio could not be decoded",
                    details={"error_type": type(exc).__name__},
                )
            decode_elapsed = time.monotonic() - decode_start

            inference_start = time.monotonic()
            try:
                chunks = backend.detect(
                    audio,
                    min_silence_duration_ms=(
                        request.parameters["min_silence_duration_ms"]
                    ),
                    speech_pad_ms=request.parameters["speech_pad_ms"],
                    sampling_rate=self.SAMPLE_RATE,
                )
                segments = self._normalize_chunks(chunks, sample_count)
            except Exception as exc:
                return self._failure(
                    request,
                    InferenceErrorCode.INFERENCE_FAILED,
                    "VAD model execution failed",
                    details={"error_type": type(exc).__name__},
                )
            inference_elapsed = time.monotonic() - inference_start

            total_sec = sample_count / self.SAMPLE_RATE
            speech_sec = sum(
                segment["duration_sec"] for segment in segments
            )
            speech_ratio = speech_sec / total_sec if total_sec else 0.0
            response = InferenceResponse(
                request_id=request.request_id,
                status=InferenceStatus.SUCCEEDED,
                outputs={
                    "segments": segments,
                    "total_sec": round(total_sec, 3),
                    "speech_sec": round(speech_sec, 3),
                    "speech_ratio": round(speech_ratio, 3),
                },
                model=EffectiveModel(
                    provider=self.PROVIDER_NAME,
                    name=self.model_name,
                    revision=self.effective_revision,
                    runtime=_runtime_name(),
                ),
                usage={
                    "audio_duration_sec": round(total_sec, 6),
                    "sample_count": sample_count,
                    "segment_count": len(segments),
                },
                timing={
                    "model_load_sec": round(load_elapsed, 6),
                    "audio_decode_sec": round(decode_elapsed, 6),
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
                    "VAD input audio artifact is missing",
                )
            if not verification.ok:
                return self._failure(
                    request,
                    InferenceErrorCode.ARTIFACT_INTEGRITY_ERROR,
                    "VAD input audio artifact failed verification",
                )
        except ArtifactNotFoundError:
            return self._failure(
                request,
                InferenceErrorCode.ARTIFACT_NOT_FOUND,
                "VAD input audio artifact is missing",
            )
        except ArtifactIntegrityError:
            return self._failure(
                request,
                InferenceErrorCode.ARTIFACT_INTEGRITY_ERROR,
                "VAD input audio artifact failed verification",
            )
        return None

    def _get_backend(self) -> tuple[VADBackend, float]:
        if self._backend is not None:
            return self._backend, 0.0
        with self._model_lock:
            if self._backend is not None:
                return self._backend, 0.0
            started = time.monotonic()
            backend, effective_revision = self._loader()
            self._backend = backend
            self.effective_revision = effective_revision
            self._load_error = None
            return backend, time.monotonic() - started

    def _validate_request(
        self,
        request: InferenceRequest,
    ) -> InferenceResponse | None:
        if request.task is not InferenceTask.VOICE_ACTIVITY_DETECTION:
            return self._failure(
                request,
                InferenceErrorCode.UNSUPPORTED_CAPABILITY,
                "local VAD provider only supports voice_activity_detection",
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
        for field_name in (
            "min_silence_duration_ms",
            "speech_pad_ms",
        ):
            value = request.parameters.get(field_name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                or value > 600_000
            ):
                return self._failure(
                    request,
                    InferenceErrorCode.INVALID_REQUEST,
                    f"parameters.{field_name} must be between 0 and 600000",
                )
        if request.parameters.get("sampling_rate") != self.SAMPLE_RATE:
            return self._failure(
                request,
                InferenceErrorCode.INVALID_REQUEST,
                "parameters.sampling_rate must be 16000",
            )
        return None

    @staticmethod
    def _normalize_chunks(
        chunks: object,
        sample_count: int,
    ) -> list[dict[str, object]]:
        if (
            isinstance(chunks, (str, bytes))
            or not isinstance(chunks, Sequence)
        ):
            raise ValueError("VAD output must be a sequence")
        segments = []
        previous_end = 0
        for index, chunk in enumerate(chunks, start=1):
            if not isinstance(chunk, Mapping):
                raise ValueError("VAD chunk must be an object")
            start_sample = _integer_sample(chunk.get("start"), "start")
            end_sample = _integer_sample(chunk.get("end"), "end")
            if (
                start_sample < previous_end
                or end_sample <= start_sample
                or end_sample > sample_count
            ):
                raise ValueError("VAD chunk timestamps are invalid")
            start_sec = round(start_sample / LocalVADProvider.SAMPLE_RATE, 3)
            end_sec = round(end_sample / LocalVADProvider.SAMPLE_RATE, 3)
            duration_sec = round(end_sec - start_sec, 3)
            if duration_sec <= 0:
                raise ValueError("VAD chunk is shorter than output precision")
            segments.append(
                {
                    "segment_id": index,
                    "start_sec": start_sec,
                    "end_sec": end_sec,
                    "duration_sec": duration_sec,
                }
            )
            previous_end = end_sample
        return segments

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


def create_local_vad_service(
    artifact_store: ArtifactStore,
    *,
    alias: str = "vad.default",
    model_name: str = "silero-vad-v6",
    revision: str | None = None,
) -> VADService:
    """Create one local VAD service with a reusable Silero model."""

    provider = LocalVADProvider(
        alias=alias,
        model_name=model_name,
        artifact_store=artifact_store,
        revision=revision,
    )
    gateway = InferenceGateway({alias: provider})
    return VADService(
        gateway,
        alias=alias,
        model_name=model_name,
        revision=provider.requested_revision,
    )
