"""Lazy local AudioSet AST provider for canonical audio events."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import threading
import time
import wave
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from importlib.metadata import PackageNotFoundError, version
from typing import BinaryIO, Protocol

import numpy as np

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
from video_preprocess.inference.audio_event import (
    AUDIO_EVENT_LABELS,
    AUDIO_EVENT_TAXONOMY_VERSION,
    DEFAULT_AUDIO_EVENT_MODEL,
    AudioEventService,
)
from video_preprocess.inference.gateway import InferenceGateway
from video_preprocess.storage import ArtifactStore
from video_preprocess.storage.errors import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
)

from .fingerprints import resolve_hf_cache_revision


DEFAULT_AUDIO_EVENT_DEVICE = "auto"
DEFAULT_AUDIO_EVENT_BATCH_SIZE = 8
AST_AUDIOSET_LABEL_COUNT = 527
AST_AUDIOSET_MAPPING_VERSION = "ast-audioset-527-to-audio-events-v1"
AST_MIN_WAVEFORM_SAMPLES = 400


class AudioFeatureExtractor(Protocol):
    """Transformers feature extractor subset used by the provider."""

    def __call__(
        self,
        raw_speech: Sequence[np.ndarray],
        *,
        sampling_rate: int,
        return_tensors: str,
    ) -> object: ...


class AudioClassificationModel(Protocol):
    """Transformers audio-classification model subset used by the provider."""

    config: object

    def __call__(self, **inputs: object) -> object: ...


ModelLoader = Callable[
    [str, str | None, str | None],
    tuple[AudioFeatureExtractor, AudioClassificationModel],
]
AudioDecoder = Callable[[BinaryIO, int], np.ndarray]
DeviceResolver = Callable[[str | None], str | None]
Classifier = Callable[
    [
        AudioFeatureExtractor,
        AudioClassificationModel,
        Sequence[np.ndarray],
        int,
        str | None,
    ],
    Sequence[Sequence[float]],
]


_EXPECTED_AUDIOSET_LABELS = {
    0: "Speech",
    16: "Laughter",
    63: "Clapping",
    67: "Applause",
    72: "Animal",
    137: "Music",
    282: "Scary music",
    300: "Vehicle",
    354: "Door",
    388: "Alarm",
    396: "Siren",
    426: "Explosion",
    513: "Noise",
    526: "Field recording",
}


def _select_auto_device(torch_runtime: object) -> str:
    cuda = getattr(torch_runtime, "cuda", None)
    cuda_available = getattr(cuda, "is_available", None)
    if callable(cuda_available) and cuda_available():
        return "cuda"
    backends = getattr(torch_runtime, "backends", None)
    mps = getattr(backends, "mps", None)
    mps_available = getattr(mps, "is_available", None)
    if callable(mps_available) and mps_available():
        return "mps"
    return "cpu"


def _default_device_resolver(device: str | None) -> str | None:
    if device != "auto":
        return device
    import torch

    return _select_auto_device(torch)


def _default_loader(
    model_name: str,
    revision: str | None,
    device: str | None,
) -> tuple[AudioFeatureExtractor, AudioClassificationModel]:
    from transformers import AutoFeatureExtractor, AutoModelForAudioClassification

    options = {}
    if revision is not None:
        options["revision"] = revision

    def load(selected_options):
        return (
            AutoFeatureExtractor.from_pretrained(
                model_name,
                **selected_options,
            ),
            AutoModelForAudioClassification.from_pretrained(
                model_name,
                **selected_options,
            ),
        )

    if not _has_cached_hf_file(model_name, "config.json", revision):
        extractor, model = load(options)
    else:
        try:
            extractor, model = load({**options, "local_files_only": True})
        except OSError:
            extractor, model = load(options)
    if device is not None:
        model = model.to(device)
    model.eval()
    return extractor, model


def _has_cached_hf_file(
    model_name: str,
    filename: str,
    revision: str | None,
) -> bool:
    """Probe cache presence without changing effective revision semantics."""

    try:
        from huggingface_hub import try_to_load_from_cache

        cached = try_to_load_from_cache(
            repo_id=model_name,
            filename=filename,
            revision=revision,
        )
    except Exception:
        return False
    return isinstance(cached, str)


def _default_decoder(stream: BinaryIO, sampling_rate: int) -> np.ndarray:
    with wave.open(stream, "rb") as wav:
        if wav.getcomptype() != "NONE":
            raise ValueError("WAV must use uncompressed PCM")
        if wav.getnchannels() != 1:
            raise ValueError("WAV must be mono")
        if wav.getsampwidth() != 2:
            raise ValueError("WAV must use signed 16-bit PCM")
        if wav.getframerate() != sampling_rate:
            raise ValueError(f"WAV must use {sampling_rate} Hz sampling")
        payload = wav.readframes(wav.getnframes())
    samples = np.frombuffer(payload, dtype="<i2")
    if samples.size == 0:
        raise ValueError("WAV must contain audio samples")
    return samples.astype(np.float32) / 32768.0


def _default_classifier(
    extractor: AudioFeatureExtractor,
    model: AudioClassificationModel,
    samples: Sequence[np.ndarray],
    sampling_rate: int,
    device: str | None,
) -> Sequence[Sequence[float]]:
    import torch

    inputs = extractor(
        samples,
        sampling_rate=sampling_rate,
        return_tensors="pt",
    )
    if device is not None and hasattr(inputs, "to"):
        inputs = inputs.to(device)
    if not isinstance(inputs, Mapping):
        raise TypeError("feature extractor output must be a mapping")
    with torch.no_grad():
        output = model(**inputs)
        logits = getattr(output, "logits", None)
        if logits is None:
            raise TypeError("audio classification output must contain logits")
        probabilities = torch.softmax(logits, dim=-1)
    return probabilities.detach().cpu().tolist()


def _runtime_name(device: str | None) -> str:
    try:
        package_version = version("transformers")
    except PackageNotFoundError:
        package_version = "unknown"
    device_name = "model_default" if device is None else device
    return (
        f"transformers/{package_version};device={device_name};"
        f"mapping={AST_AUDIOSET_MAPPING_VERSION}"
    )


def _canonical_label(index: int) -> str | None:
    if index in range(16, 22):
        return "laughter"
    if index in {63, 67}:
        return "applause"
    if index in range(27, 38) or index in range(137, 283):
        return "music"
    if index in range(72, 137):
        return "animal"
    if index in {310, 388, 395, 398, 399, 400}:
        return "alarm"
    if index in {323, 324, 325, 396, 397}:
        return "siren"
    if index in range(300, 343):
        return "vehicle"
    if index in range(354, 364):
        return "door"
    if (
        index == 419
        or index in range(426, 444)
        or index in {460, 461}
        or index in range(465, 471)
    ):
        return "impact"
    if index in range(513, 524):
        return "noise"
    return None


def build_audioset_label_mapping(id2label: object) -> dict[int, str]:
    """Validate the selected model label space and return canonical targets."""

    if not isinstance(id2label, Mapping):
        raise ValueError("model config.id2label must be a mapping")
    normalized: dict[int, str] = {}
    for raw_index, raw_label in id2label.items():
        try:
            index = int(raw_index)
        except (TypeError, ValueError) as exc:
            raise ValueError("AudioSet label index must be an integer") from exc
        if not isinstance(raw_label, str) or not raw_label:
            raise ValueError("AudioSet display label must be a non-empty string")
        normalized[index] = raw_label
    if len(normalized) != AST_AUDIOSET_LABEL_COUNT or set(normalized) != set(
        range(AST_AUDIOSET_LABEL_COUNT)
    ):
        raise ValueError("model must expose the 527-label AudioSet index")
    for index, expected in _EXPECTED_AUDIOSET_LABELS.items():
        if normalized[index] != expected:
            raise ValueError(
                "model AudioSet label order does not match the supported mapping"
            )
    return {
        index: canonical
        for index in range(AST_AUDIOSET_LABEL_COUNT)
        if (canonical := _canonical_label(index)) is not None
    }


class LocalAudioEventProvider:
    """Classify ArtifactRef WAV windows with one reusable local AST model."""

    PROVIDER_NAME = "local.audio-event"
    SAMPLE_RATE = 16000
    INPUT_MEDIA_TYPES = ("audio/wav", "audio/x-wav")

    def __init__(
        self,
        *,
        alias: str,
        model_name: str,
        artifact_store: ArtifactStore,
        revision: str | None = None,
        device: str | None = DEFAULT_AUDIO_EVENT_DEVICE,
        max_batch_size: int = DEFAULT_AUDIO_EVENT_BATCH_SIZE,
        max_artifact_bytes: int = 4 * 1024 * 1024 * 1024,
        loader: ModelLoader = _default_loader,
        decoder: AudioDecoder = _default_decoder,
        device_resolver: DeviceResolver = _default_device_resolver,
        classifier: Classifier = _default_classifier,
    ) -> None:
        for value, field_name in ((alias, "alias"), (model_name, "model_name")):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        if revision is not None and (
            not isinstance(revision, str) or not revision.strip()
        ):
            raise ValueError("revision must be a non-empty string or None")
        if device is not None and (
            not isinstance(device, str) or not device.strip()
        ):
            raise ValueError("device must be a non-empty string or None")
        for value, field_name in (
            (max_batch_size, "max_batch_size"),
            (max_artifact_bytes, "max_artifact_bytes"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{field_name} must be at least 1")
        for callback, field_name in (
            (loader, "loader"),
            (decoder, "decoder"),
            (device_resolver, "device_resolver"),
            (classifier, "classifier"),
        ):
            if not callable(callback):
                raise TypeError(f"{field_name} must be callable")

        self.alias = alias.strip()
        self.model_name = model_name.strip()
        self.artifact_store = artifact_store
        self.revision = revision
        self.requested_revision = revision or "default"
        self.effective_revision = self.requested_revision
        normalized_device = None if device is None else device.strip()
        self.device = (
            "auto"
            if normalized_device is not None
            and normalized_device.lower() == "auto"
            else normalized_device
        )
        self.max_batch_size = max_batch_size
        self.max_artifact_bytes = max_artifact_bytes
        self._loader = loader
        self._decoder = decoder
        self._device_resolver = device_resolver
        self._classifier = classifier
        self._resolved_device: str | None = None
        self._device_is_resolved = False
        self._device_lock = threading.Lock()
        self._extractor: AudioFeatureExtractor | None = None
        self._model: AudioClassificationModel | None = None
        self._label_mapping: dict[int, str] | None = None
        self._model_lock = threading.Lock()
        self._inference_lock = threading.Lock()
        self._load_error: str | None = None
        self._decoded_audio: dict[str, np.ndarray] = {}
        self._responses: dict[str, tuple[str, InferenceResponse]] = {}

    @property
    def is_loaded(self) -> bool:
        return self._extractor is not None and self._model is not None

    async def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.PROVIDER_NAME,
            tasks=[InferenceTask.AUDIO_EVENT_DETECTION],
            model_aliases=[self.alias],
            input_media_types=self.INPUT_MEDIA_TYPES,
            features=[
                "window_batch",
                AUDIO_EVENT_TAXONOMY_VERSION,
                AST_AUDIOSET_MAPPING_VERSION,
                "automatic_device_selection",
                "softmax_confidence",
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
                "requested_device": self.device or "model_default",
                "resolved_device": (
                    self._device_name(self._resolved_device)
                    if self._device_is_resolved
                    else None
                ),
                "mapping_version": AST_AUDIOSET_MAPPING_VERSION,
            },
        )

    async def effective_model(self) -> EffectiveModel | None:
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
        resolved_device = await asyncio.to_thread(self._get_device)
        return EffectiveModel(
            provider=self.PROVIDER_NAME,
            name=self.model_name,
            revision=revision,
            runtime=_runtime_name(resolved_device),
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
                return self._with_request_id(cached_response, request.request_id)

            validation_error = self._validate_request(request)
            if validation_error is not None:
                return validation_error
            audio = request.inputs["audio"]
            windows = request.inputs["windows"]
            sampling_rate = request.parameters["sampling_rate"]
            labels = request.parameters["labels"]
            min_confidence = float(request.parameters["min_confidence"])
            assert isinstance(audio, ArtifactRef)
            assert isinstance(windows, list)
            assert isinstance(sampling_rate, int)
            assert isinstance(labels, list)

            decode_started = time.monotonic()
            decoded = self._load_audio(request, audio, sampling_rate)
            if isinstance(decoded, InferenceResponse):
                return decoded
            decode_elapsed = time.monotonic() - decode_started
            try:
                window_samples = self._window_samples(
                    decoded,
                    windows,
                    sampling_rate,
                )
            except ValueError as exc:
                return self._failure(
                    request,
                    InferenceErrorCode.INVALID_REQUEST,
                    str(exc),
                )

            try:
                extractor, model, mapping, load_elapsed = self._get_model()
            except Exception as exc:
                self._load_error = type(exc).__name__
                return self._failure(
                    request,
                    InferenceErrorCode.MODEL_UNAVAILABLE,
                    "audio event model could not be loaded",
                    details={"error_type": type(exc).__name__},
                )

            inference_started = time.monotonic()
            try:
                resolved_device = self._get_device()
                scores = self._classifier(
                    extractor,
                    model,
                    window_samples,
                    sampling_rate,
                    resolved_device,
                )
                results = self._results(
                    scores,
                    windows=windows,
                    labels=labels,
                    min_confidence=min_confidence,
                    mapping=mapping,
                )
            except Exception as exc:
                return self._failure(
                    request,
                    InferenceErrorCode.INFERENCE_FAILED,
                    "audio event model execution failed",
                    details=self._execution_error_details(exc),
                )
            inference_elapsed = time.monotonic() - inference_started
            response = InferenceResponse(
                request_id=request.request_id,
                status=InferenceStatus.SUCCEEDED,
                outputs={"results": results},
                model=EffectiveModel(
                    provider=self.PROVIDER_NAME,
                    name=self.model_name,
                    revision=self.effective_revision,
                    runtime=_runtime_name(resolved_device),
                ),
                usage={
                    "window_count": len(windows),
                    "batch_size": len(windows),
                    "sampling_rate": sampling_rate,
                    "source_label_count": AST_AUDIOSET_LABEL_COUNT,
                    "mapping_version": AST_AUDIOSET_MAPPING_VERSION,
                    "device": self._device_name(resolved_device),
                },
                timing={
                    "decode_sec": round(decode_elapsed, 6),
                    "model_load_sec": round(load_elapsed, 6),
                    "inference_sec": round(inference_elapsed, 6),
                },
            )
            self._cache_response(request, fingerprint, response)
            return response

    def _load_audio(
        self,
        request: InferenceRequest,
        artifact: ArtifactRef,
        sampling_rate: int,
    ) -> np.ndarray | InferenceResponse:
        try:
            verification = self.artifact_store.verify(artifact)
            if not verification.exists:
                return self._failure(
                    request,
                    InferenceErrorCode.ARTIFACT_NOT_FOUND,
                    "audio event input artifact is missing",
                )
            if not verification.ok:
                return self._failure(
                    request,
                    InferenceErrorCode.ARTIFACT_INTEGRITY_ERROR,
                    "audio event input artifact failed verification",
                )
            cache_key = (
                f"{artifact.checksum.algorithm}:"
                f"{artifact.checksum.value}:{artifact.size_bytes}"
            )
            cached = self._decoded_audio.get(cache_key)
            if cached is not None:
                return cached
            with self.artifact_store.open(artifact) as stream:
                decoded = self._decoder(stream, sampling_rate)
            if len(self._decoded_audio) >= 4:
                self._decoded_audio.pop(next(iter(self._decoded_audio)))
            self._decoded_audio[cache_key] = decoded
            return decoded
        except ArtifactNotFoundError:
            return self._failure(
                request,
                InferenceErrorCode.ARTIFACT_NOT_FOUND,
                "audio event input artifact is missing",
            )
        except ArtifactIntegrityError:
            return self._failure(
                request,
                InferenceErrorCode.ARTIFACT_INTEGRITY_ERROR,
                "audio event input artifact failed verification",
            )
        except Exception as exc:
            return self._failure(
                request,
                InferenceErrorCode.INVALID_REQUEST,
                "audio event input WAV could not be decoded",
                details={"error_type": type(exc).__name__},
            )

    def _get_model(
        self,
    ) -> tuple[
        AudioFeatureExtractor,
        AudioClassificationModel,
        dict[int, str],
        float,
    ]:
        if (
            self._extractor is not None
            and self._model is not None
            and self._label_mapping is not None
        ):
            return self._extractor, self._model, self._label_mapping, 0.0
        with self._model_lock:
            if (
                self._extractor is not None
                and self._model is not None
                and self._label_mapping is not None
            ):
                return self._extractor, self._model, self._label_mapping, 0.0
            started = time.monotonic()
            resolved_device = self._get_device()
            extractor, model = self._loader(
                self.model_name,
                self.revision,
                resolved_device,
            )
            mapping = build_audioset_label_mapping(
                getattr(getattr(model, "config", None), "id2label", None)
            )
            self._extractor = extractor
            self._model = model
            self._label_mapping = mapping
            self.effective_revision = self._resolve_revision(
                extractor,
                model,
                fallback=self.requested_revision,
            )
            self._load_error = None
            return extractor, model, mapping, time.monotonic() - started

    def _get_device(self) -> str | None:
        if self._device_is_resolved:
            return self._resolved_device
        with self._device_lock:
            if self._device_is_resolved:
                return self._resolved_device
            resolved = self._device_resolver(self.device)
            if resolved is not None and (
                not isinstance(resolved, str) or not resolved.strip()
            ):
                raise ValueError(
                    "device_resolver must return a non-empty string or None"
                )
            self._resolved_device = None if resolved is None else resolved.strip()
            self._device_is_resolved = True
            return self._resolved_device

    def _validate_request(
        self,
        request: InferenceRequest,
    ) -> InferenceResponse | None:
        if request.task is not InferenceTask.AUDIO_EVENT_DETECTION:
            return self._failure(
                request,
                InferenceErrorCode.UNSUPPORTED_CAPABILITY,
                "local audio event provider only supports audio_event_detection",
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
        if audio.media_type not in self.INPUT_MEDIA_TYPES:
            return self._failure(
                request,
                InferenceErrorCode.INVALID_REQUEST,
                "inputs.audio has an unsupported media type",
                details={"media_type": audio.media_type},
            )
        if audio.size_bytes > self.max_artifact_bytes:
            return self._failure(
                request,
                InferenceErrorCode.INVALID_REQUEST,
                "audio event artifact exceeds provider maximum",
                details={"max_artifact_bytes": self.max_artifact_bytes},
            )
        windows = request.inputs.get("windows")
        if not isinstance(windows, list) or not windows:
            return self._failure(
                request,
                InferenceErrorCode.INVALID_REQUEST,
                "inputs.windows must be a non-empty array",
            )
        if len(windows) > self.max_batch_size:
            return self._failure(
                request,
                InferenceErrorCode.INVALID_REQUEST,
                "audio event batch exceeds provider maximum",
                details={"max_batch_size": self.max_batch_size},
            )
        previous_id = None
        for index, window in enumerate(windows):
            if not isinstance(window, Mapping):
                return self._failure(
                    request,
                    InferenceErrorCode.INVALID_REQUEST,
                    f"inputs.windows[{index}] must be an object",
                )
            window_id = window.get("window_id")
            start_sec = window.get("start_sec")
            end_sec = window.get("end_sec")
            if (
                isinstance(window_id, bool)
                or not isinstance(window_id, int)
                or window_id < 1
                or (
                    previous_id is not None
                    and window_id != previous_id + 1
                )
                or not self._finite_interval(start_sec, end_sec)
            ):
                return self._failure(
                    request,
                    InferenceErrorCode.INVALID_REQUEST,
                    f"inputs.windows[{index}] is invalid or out of order",
                )
            previous_id = window_id
        parameters = request.parameters
        if parameters.get("taxonomy_version") != AUDIO_EVENT_TAXONOMY_VERSION:
            return self._failure(
                request,
                InferenceErrorCode.INVALID_REQUEST,
                "parameters.taxonomy_version is unsupported",
            )
        labels = parameters.get("labels")
        if (
            not isinstance(labels, list)
            or not labels
            or any(
                not isinstance(label, str)
                or label not in AUDIO_EVENT_LABELS
                for label in labels
            )
            or len(labels) != len(set(labels))
        ):
            return self._failure(
                request,
                InferenceErrorCode.INVALID_REQUEST,
                "parameters.labels must be unique audio-events-v1 labels",
            )
        confidence = parameters.get("min_confidence")
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(float(confidence))
            or not 0 <= float(confidence) <= 1
        ):
            return self._failure(
                request,
                InferenceErrorCode.INVALID_REQUEST,
                "parameters.min_confidence must be between 0 and 1",
            )
        if parameters.get("sampling_rate") != self.SAMPLE_RATE:
            return self._failure(
                request,
                InferenceErrorCode.INVALID_REQUEST,
                f"parameters.sampling_rate must be {self.SAMPLE_RATE}",
            )
        if parameters.get("interval") != "half-open":
            return self._failure(
                request,
                InferenceErrorCode.INVALID_REQUEST,
                "parameters.interval must be half-open",
            )
        return None

    @staticmethod
    def _finite_interval(start_sec: object, end_sec: object) -> bool:
        for value in (start_sec, end_sec):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                return False
        return 0 <= float(start_sec) < float(end_sec)

    @staticmethod
    def _window_samples(
        decoded: np.ndarray,
        windows: Sequence[Mapping[str, object]],
        sampling_rate: int,
    ) -> list[np.ndarray]:
        values = []
        end_tolerance = max(1, int(round(sampling_rate * 0.01)))
        for index, window in enumerate(windows):
            start = int(round(float(window["start_sec"]) * sampling_rate))
            end = int(round(float(window["end_sec"]) * sampling_rate))
            if end > len(decoded) and end - len(decoded) <= end_tolerance:
                end = len(decoded)
            if start < 0 or end <= start or end > len(decoded):
                raise ValueError(
                    f"inputs.windows[{index}] exceeds decoded audio duration"
                )
            samples = decoded[start:end]
            if len(samples) < AST_MIN_WAVEFORM_SAMPLES:
                samples = np.pad(
                    samples,
                    (0, AST_MIN_WAVEFORM_SAMPLES - len(samples)),
                )
            values.append(samples)
        return values

    @staticmethod
    def _results(
        scores: object,
        *,
        windows: Sequence[Mapping[str, object]],
        labels: Sequence[str],
        min_confidence: float,
        mapping: Mapping[int, str],
    ) -> list[dict[str, object]]:
        if (
            isinstance(scores, (str, bytes))
            or not isinstance(scores, Sequence)
            or len(scores) != len(windows)
        ):
            raise ValueError("classifier result count does not match windows")
        results = []
        for result_index, (window, row) in enumerate(zip(windows, scores)):
            if (
                isinstance(row, (str, bytes))
                or not isinstance(row, Sequence)
                or len(row) != AST_AUDIOSET_LABEL_COUNT
            ):
                raise ValueError(
                    f"classifier result {result_index} must contain 527 scores"
                )
            canonical_scores = {label: 0.0 for label in labels}
            for source_index, canonical in mapping.items():
                if canonical not in canonical_scores:
                    continue
                value = row[source_index]
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    or not 0 <= float(value) <= 1
                ):
                    raise ValueError("classifier confidence must be between 0 and 1")
                canonical_scores[canonical] = max(
                    canonical_scores[canonical],
                    float(value),
                )
            results.append({
                "window_id": window["window_id"],
                "labels": [
                    {"label": label, "confidence": round(canonical_scores[label], 6)}
                    for label in labels
                    if canonical_scores[label] >= min_confidence
                ],
            })
        return results

    def _execution_error_details(self, exc: Exception) -> dict[str, object]:
        details = {
            "error_type": type(exc).__name__,
            "device": self._device_name(self._resolved_device),
        }
        message = str(exc).lower()
        if (
            "outofmemory" in type(exc).__name__.lower()
            or "out of memory" in message
        ):
            details.update({
                "reason": "DEVICE_OUT_OF_MEMORY",
                "max_batch_size": self.max_batch_size,
            })
        return details

    @staticmethod
    def _device_name(device: str | None) -> str:
        return "model_default" if device is None else device

    @staticmethod
    def _resolve_revision(
        extractor: AudioFeatureExtractor,
        model: AudioClassificationModel,
        *,
        fallback: str,
    ) -> str:
        candidates = [
            getattr(getattr(model, "config", None), "_commit_hash", None),
            getattr(extractor, "_commit_hash", None),
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
        return hashlib.sha256(json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()

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


def create_local_audio_event_service(
    model_name: str,
    artifact_store: ArtifactStore,
    *,
    alias: str = "audio_event.default",
    revision: str | None = None,
    device: str | None = DEFAULT_AUDIO_EVENT_DEVICE,
    max_batch_size: int = DEFAULT_AUDIO_EVENT_BATCH_SIZE,
) -> AudioEventService:
    """Create an audio-event service with one reusable local AST model."""

    provider = LocalAudioEventProvider(
        alias=alias,
        model_name=model_name,
        artifact_store=artifact_store,
        revision=revision,
        device=device,
        max_batch_size=max_batch_size,
    )
    return AudioEventService(
        InferenceGateway({alias: provider}),
        alias=alias,
        model_name=model_name,
        revision=provider.requested_revision,
        batch_size=max_batch_size,
    )
