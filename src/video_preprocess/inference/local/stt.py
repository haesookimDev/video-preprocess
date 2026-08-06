"""Lazy, reusable faster-whisper speech-to-text provider."""

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
from video_preprocess.inference.stt import STTService, SpeechChunk
from video_preprocess.storage import ArtifactStore
from video_preprocess.storage.errors import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
)


class AudioBuffer(Protocol):
    """Sliceable decoded mono audio used by faster-whisper."""

    def __len__(self) -> int: ...

    def __getitem__(self, item: slice) -> object: ...


class STTModel(Protocol):
    """Subset of WhisperModel required by this provider."""

    def transcribe(
        self,
        audio: object,
        *,
        language: str | None,
        beam_size: int,
    ) -> tuple[object, object]: ...


ModelLoader = Callable[
    [str, str | None, str, str],
    tuple[STTModel, str],
]
AudioDecoder = Callable[[BinaryIO, int], AudioBuffer]


def _snapshot_revision(path: str, fallback: str) -> str:
    parts = Path(path).resolve().parts
    for index, part in enumerate(parts[:-1]):
        if part == "snapshots":
            candidate = parts[index + 1]
            if candidate:
                return candidate
    return fallback


def _default_loader(
    model_name: str,
    revision: str | None,
    device: str,
    compute_type: str,
) -> tuple[STTModel, str]:
    from faster_whisper import WhisperModel
    from faster_whisper.utils import download_model

    requested_revision = revision or "default"
    if Path(model_name).is_dir():
        model_path = model_name
    else:
        model_path = download_model(model_name, revision=revision)
    resolved_revision = _snapshot_revision(model_path, requested_revision)
    model = WhisperModel(
        model_path,
        device=device,
        compute_type=compute_type,
    )
    return model, resolved_revision


def _default_decoder(stream: BinaryIO, sampling_rate: int) -> AudioBuffer:
    from faster_whisper.audio import decode_audio

    return decode_audio(stream, sampling_rate=sampling_rate)


def _runtime_name() -> str:
    try:
        whisper_version = version("faster-whisper")
    except PackageNotFoundError:
        whisper_version = "unknown"
    try:
        ctranslate_version = version("ctranslate2")
    except PackageNotFoundError:
        ctranslate_version = "unknown"
    return (
        f"faster-whisper/{whisper_version};"
        f"ctranslate2/{ctranslate_version}"
    )


def _finite_number(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be finite")
    return number


class LocalSTTProvider:
    """In-process faster-whisper provider using one audio ArtifactRef."""

    PROVIDER_NAME = "local.stt"
    SAMPLE_RATE = 16000

    def __init__(
        self,
        *,
        alias: str,
        model_name: str,
        artifact_store: ArtifactStore,
        revision: str | None = None,
        device: str = "auto",
        compute_type: str = "int8",
        max_batch_size: int = 256,
        max_artifact_bytes: int = 4 * 1024 * 1024 * 1024,
        loader: ModelLoader = _default_loader,
        decoder: AudioDecoder = _default_decoder,
    ) -> None:
        if (
            not isinstance(alias, str)
            or not alias.strip()
            or not isinstance(model_name, str)
            or not model_name.strip()
        ):
            raise ValueError("alias and model_name must be non-empty")
        for field_name, value in (
            ("device", device),
            ("compute_type", compute_type),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        if revision is not None and (
            not isinstance(revision, str) or not revision.strip()
        ):
            raise ValueError("revision must be a non-empty string or None")
        for field_name, value in (
            ("max_batch_size", max_batch_size),
            ("max_artifact_bytes", max_artifact_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{field_name} must be at least 1")
        self.alias = alias
        self.model_name = model_name
        self.artifact_store = artifact_store
        self.revision = revision
        self.requested_revision = revision or "default"
        self.effective_revision = self.requested_revision
        self.device = device
        self.compute_type = compute_type
        self.max_batch_size = max_batch_size
        self.max_artifact_bytes = max_artifact_bytes
        self._loader = loader
        self._decoder = decoder
        self._model: STTModel | None = None
        self._model_lock = threading.Lock()
        self._inference_lock = threading.Lock()
        self._load_error: str | None = None
        self._responses: dict[str, tuple[str, InferenceResponse]] = {}

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    async def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.PROVIDER_NAME,
            tasks=[InferenceTask.SPEECH_TO_TEXT],
            model_aliases=[self.alias],
            input_media_types=["audio/wav"],
            features=[
                "absolute_segment_timestamps",
                "language_detection",
                "vad_chunks",
            ],
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
            details={
                "model_loaded": self.is_loaded,
                "device": self.device,
                "compute_type": self.compute_type,
            },
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

            audio_ref = request.inputs["audio"]
            chunks = request.inputs["chunks"]
            assert isinstance(audio_ref, ArtifactRef)
            assert isinstance(chunks, list)
            sampling_rate = request.parameters["sampling_rate"]
            assert isinstance(sampling_rate, int)

            decode_start = time.monotonic()
            decoded = self._decode_audio(request, audio_ref, sampling_rate)
            if isinstance(decoded, InferenceResponse):
                return decoded
            decode_elapsed = time.monotonic() - decode_start
            try:
                sample_count = len(decoded)
            except Exception as exc:
                return self._failure(
                    request,
                    InferenceErrorCode.INVALID_REQUEST,
                    "decoded audio is not a sliceable mono buffer",
                    details={"error_type": type(exc).__name__},
                )

            normalized_chunks = []
            previous_end = 0.0
            for index, raw_chunk in enumerate(chunks):
                try:
                    chunk = SpeechChunk.from_value(raw_chunk, index)
                except ValueError as exc:
                    return self._failure(
                        request,
                        InferenceErrorCode.INVALID_REQUEST,
                        str(exc),
                    )
                start_sample = int(chunk.start_sec * sampling_rate)
                end_sample = int(chunk.end_sec * sampling_rate)
                if index > 0 and chunk.start_sec < previous_end:
                    return self._failure(
                        request,
                        InferenceErrorCode.INVALID_REQUEST,
                        "chunks must be ordered and non-overlapping",
                    )
                if end_sample > sample_count or end_sample <= start_sample:
                    return self._failure(
                        request,
                        InferenceErrorCode.INVALID_REQUEST,
                        f"chunks[{index}] is outside decoded audio bounds",
                        details={"sample_count": sample_count},
                    )
                normalized_chunks.append(
                    (chunk, decoded[start_sample:end_sample])
                )
                previous_end = chunk.end_sec

            try:
                model, load_elapsed = self._get_model()
            except Exception as exc:
                self._load_error = type(exc).__name__
                return self._failure(
                    request,
                    InferenceErrorCode.MODEL_UNAVAILABLE,
                    "STT model could not be loaded",
                    details={"error_type": type(exc).__name__},
                )

            inference_start = time.monotonic()
            transcript = []
            detected_language = None
            language_probability = None
            try:
                for chunk, audio_slice in normalized_chunks:
                    raw_segments, info = model.transcribe(
                        audio_slice,
                        language=request.parameters.get("language"),
                        beam_size=request.parameters["beam_size"],
                    )
                    if detected_language is None:
                        detected_language, language_probability = (
                            self._normalize_language_info(info)
                        )
                    for raw_segment in raw_segments:
                        transcript.append(
                            self._normalize_segment(raw_segment, chunk)
                        )
            except Exception as exc:
                return self._failure(
                    request,
                    InferenceErrorCode.INFERENCE_FAILED,
                    "STT model execution failed",
                    details={"error_type": type(exc).__name__},
                )
            inference_elapsed = time.monotonic() - inference_start
            speech_duration = sum(
                chunk.end_sec - chunk.start_sec
                for chunk, _ in normalized_chunks
            )
            response = InferenceResponse(
                request_id=request.request_id,
                status=InferenceStatus.SUCCEEDED,
                outputs={
                    "segments": transcript,
                    "language": detected_language,
                    "language_probability": language_probability,
                },
                model=EffectiveModel(
                    provider=self.PROVIDER_NAME,
                    name=self.model_name,
                    revision=self.effective_revision,
                    runtime=_runtime_name(),
                ),
                usage={
                    "audio_duration_sec": round(
                        sample_count / sampling_rate,
                        6,
                    ),
                    "speech_duration_sec": round(speech_duration, 6),
                    "chunk_count": len(normalized_chunks),
                    "segment_count": len(transcript),
                },
                timing={
                    "audio_decode_sec": round(decode_elapsed, 6),
                    "model_load_sec": round(load_elapsed, 6),
                    "inference_sec": round(inference_elapsed, 6),
                },
            )
            self._cache_response(request, fingerprint, response)
            return response

    def _decode_audio(
        self,
        request: InferenceRequest,
        artifact: ArtifactRef,
        sampling_rate: int,
    ) -> AudioBuffer | InferenceResponse:
        try:
            verification = self.artifact_store.verify(artifact)
            if not verification.exists:
                return self._failure(
                    request,
                    InferenceErrorCode.ARTIFACT_NOT_FOUND,
                    "STT input audio artifact is missing",
                )
            if not verification.ok:
                return self._failure(
                    request,
                    InferenceErrorCode.ARTIFACT_INTEGRITY_ERROR,
                    "STT input audio artifact failed verification",
                )
            with self.artifact_store.open(artifact) as stream:
                return self._decoder(stream, sampling_rate)
        except ArtifactNotFoundError:
            return self._failure(
                request,
                InferenceErrorCode.ARTIFACT_NOT_FOUND,
                "STT input audio artifact is missing",
            )
        except ArtifactIntegrityError:
            return self._failure(
                request,
                InferenceErrorCode.ARTIFACT_INTEGRITY_ERROR,
                "STT input audio artifact failed verification",
            )
        except Exception as exc:
            return self._failure(
                request,
                InferenceErrorCode.INVALID_REQUEST,
                "STT input audio could not be decoded",
                details={"error_type": type(exc).__name__},
            )

    def _get_model(self) -> tuple[STTModel, float]:
        if self._model is not None:
            return self._model, 0.0
        with self._model_lock:
            if self._model is not None:
                return self._model, 0.0
            started = time.monotonic()
            model, effective_revision = self._loader(
                self.model_name,
                self.revision,
                self.device,
                self.compute_type,
            )
            self._model = model
            self.effective_revision = effective_revision
            self._load_error = None
            return model, time.monotonic() - started

    def _validate_request(
        self,
        request: InferenceRequest,
    ) -> InferenceResponse | None:
        if request.task is not InferenceTask.SPEECH_TO_TEXT:
            return self._failure(
                request,
                InferenceErrorCode.UNSUPPORTED_CAPABILITY,
                "local STT provider only supports speech_to_text",
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
        chunks = request.inputs.get("chunks")
        if not isinstance(chunks, list) or not chunks:
            return self._failure(
                request,
                InferenceErrorCode.INVALID_REQUEST,
                "inputs.chunks must be a non-empty array",
            )
        if len(chunks) > self.max_batch_size:
            return self._failure(
                request,
                InferenceErrorCode.INVALID_REQUEST,
                "STT chunk batch exceeds provider maximum",
                details={"max_batch_size": self.max_batch_size},
            )
        sampling_rate = request.parameters.get("sampling_rate")
        if sampling_rate != self.SAMPLE_RATE:
            return self._failure(
                request,
                InferenceErrorCode.INVALID_REQUEST,
                "parameters.sampling_rate must be 16000",
            )
        language = request.parameters.get("language")
        if language is not None and (
            not isinstance(language, str) or not language.strip()
        ):
            return self._failure(
                request,
                InferenceErrorCode.INVALID_REQUEST,
                "parameters.language must be a non-empty string or null",
            )
        beam_size = request.parameters.get("beam_size")
        if (
            isinstance(beam_size, bool)
            or not isinstance(beam_size, int)
            or beam_size < 1
            or beam_size > 100
        ):
            return self._failure(
                request,
                InferenceErrorCode.INVALID_REQUEST,
                "parameters.beam_size must be between 1 and 100",
            )
        return None

    @staticmethod
    def _normalize_language_info(info: object) -> tuple[str, float]:
        language = getattr(info, "language", None)
        if not isinstance(language, str) or not language.strip():
            raise ValueError("STT language result is invalid")
        probability = _finite_number(
            getattr(info, "language_probability", None),
            "language_probability",
        )
        if probability < 0 or probability > 1:
            raise ValueError("language_probability must be between 0 and 1")
        return language.strip(), probability

    @staticmethod
    def _normalize_segment(
        segment: object,
        chunk: SpeechChunk,
    ) -> dict[str, object]:
        start = _finite_number(getattr(segment, "start", None), "start")
        end = _finite_number(getattr(segment, "end", None), "end")
        text = getattr(segment, "text", None)
        avg_logprob = _finite_number(
            getattr(segment, "avg_logprob", None),
            "avg_logprob",
        )
        no_speech_prob = _finite_number(
            getattr(segment, "no_speech_prob", None),
            "no_speech_prob",
        )
        if start < 0 or end < start:
            raise ValueError("STT segment timestamps are invalid")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("STT segment text is empty")
        if no_speech_prob < 0 or no_speech_prob > 1:
            raise ValueError("no_speech_prob must be between 0 and 1")
        return {
            "start_sec": round(chunk.start_sec + start, 3),
            "end_sec": round(chunk.start_sec + end, 3),
            "text": text.strip(),
            "avg_logprob": round(avg_logprob, 4),
            "no_speech_prob": round(no_speech_prob, 4),
            "vad_source_ids": list(chunk.source_ids),
        }

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


def create_local_stt_service(
    model_name: str,
    artifact_store: ArtifactStore,
    *,
    alias: str = "stt.default",
    revision: str | None = None,
    device: str = "auto",
    compute_type: str = "int8",
) -> STTService:
    """Create one local STT service with a reusable Whisper model."""

    provider = LocalSTTProvider(
        alias=alias,
        model_name=model_name,
        artifact_store=artifact_store,
        revision=revision,
        device=device,
        compute_type=compute_type,
    )
    gateway = InferenceGateway({alias: provider})
    return STTService(
        gateway,
        alias=alias,
        model_name=model_name,
        revision=provider.requested_revision,
    )
