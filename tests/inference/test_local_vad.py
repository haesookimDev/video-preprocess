"""Tests for the ArtifactRef-backed local VAD provider."""

import asyncio
import io
from pathlib import Path

import pytest

from video_preprocess.domain import (
    HealthState,
    InferenceErrorCode,
    InferenceTask,
)
from video_preprocess.inference import InferenceCallError, VADService
from video_preprocess.inference.gateway import InferenceGateway
from video_preprocess.inference.local import LocalVADProvider
from video_preprocess.storage import LocalArtifactStore


class FakeBackend:
    def __init__(self, sample_count: int = 48000) -> None:
        self.sample_count = sample_count
        self.decode_count = 0
        self.detect_count = 0
        self.options = []

    def decode(self, stream, sampling_rate):
        self.decode_count += 1
        assert stream.read() == b"audio"
        assert sampling_rate == 16000
        return [0.0] * self.sample_count

    def detect(
        self,
        audio,
        *,
        min_silence_duration_ms,
        speech_pad_ms,
        sampling_rate,
    ):
        self.detect_count += 1
        assert len(audio) == self.sample_count
        assert sampling_rate == 16000
        self.options.append((min_silence_duration_ms, speech_pad_ms))
        return [
            {"start": 8000, "end": 16000},
            {"start": 24000, "end": 32000},
        ]


def publish_audio(store: LocalArtifactStore, name: str = "audio"):
    pending = store.put(
        io.BytesIO(b"audio"),
        artifact_id=f"art_{name}",
        relative_path=f"audio/{name}.wav",
        kind="audio",
        media_type="audio/wav",
    )
    return store.publish(pending)


def make_service(store: LocalArtifactStore, *, loader):
    provider = LocalVADProvider(
        alias="vad.default",
        model_name="silero-vad-v6",
        artifact_store=store,
        loader=loader,
    )
    service = VADService(
        InferenceGateway({"vad.default": provider}),
        alias="vad.default",
        model_name="silero-vad-v6",
        revision=provider.requested_revision,
    )
    return provider, service


def test_provider_returns_segments_and_reuses_backend_and_result(
    tmp_path: Path,
) -> None:
    store = LocalArtifactStore(tmp_path, namespace="run_123")
    audio = publish_audio(store)
    backend = FakeBackend()
    load_count = 0

    def loader():
        nonlocal load_count
        load_count += 1
        return backend, "sha256:model123"

    provider, service = make_service(store, loader=loader)

    first = service.detect(
        audio,
        min_silence_duration_ms=500,
        speech_pad_ms=200,
    )
    second = service.detect(
        audio,
        min_silence_duration_ms=500,
        speech_pad_ms=200,
    )

    assert [segment.to_dict() for segment in first.segments] == [
        {
            "segment_id": 1,
            "start_sec": 0.5,
            "end_sec": 1.0,
            "duration_sec": 0.5,
        },
        {
            "segment_id": 2,
            "start_sec": 1.5,
            "end_sec": 2.0,
            "duration_sec": 0.5,
        },
    ]
    assert second.segments == first.segments
    assert first.total_sec == 3.0
    assert first.speech_sec == 1.0
    assert first.speech_ratio == 0.333
    assert first.model.provider == "local.vad"
    assert first.model.revision == "sha256:model123"
    assert first.usage["sample_count"] == 48000
    assert backend.options == [(500, 200)]
    assert backend.decode_count == 1
    assert backend.detect_count == 1
    assert load_count == 1
    assert provider.is_loaded


def test_provider_reuses_backend_for_different_options(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path, namespace="run_123")
    audio = publish_audio(store)
    backend = FakeBackend()
    provider, service = make_service(
        store,
        loader=lambda: (backend, "sha256:model123"),
    )

    service.detect(audio, min_silence_duration_ms=500, speech_pad_ms=200)
    service.detect(audio, min_silence_duration_ms=750, speech_pad_ms=100)

    assert provider.is_loaded
    assert backend.options == [(500, 200), (750, 100)]
    assert backend.decode_count == 2
    assert backend.detect_count == 2


def test_provider_rejects_missing_and_corrupt_audio(
    tmp_path: Path,
) -> None:
    store = LocalArtifactStore(tmp_path, namespace="run_123")
    missing = publish_audio(store, "missing")
    corrupt = publish_audio(store, "corrupt")
    (tmp_path / "audio" / "missing.wav").unlink()
    (tmp_path / "audio" / "corrupt.wav").write_bytes(b"changed")
    _, service = make_service(
        store,
        loader=lambda: (FakeBackend(), "sha256:model123"),
    )

    with pytest.raises(InferenceCallError) as missing_error:
        service.detect(missing)
    with pytest.raises(InferenceCallError) as corrupt_error:
        service.detect(corrupt)

    assert missing_error.value.failure.code is InferenceErrorCode.ARTIFACT_NOT_FOUND
    assert (
        corrupt_error.value.failure.code
        is InferenceErrorCode.ARTIFACT_INTEGRITY_ERROR
    )


def test_provider_normalizes_load_decode_and_detection_failures(
    tmp_path: Path,
) -> None:
    store = LocalArtifactStore(tmp_path, namespace="run_123")
    audio = publish_audio(store)

    def fail_load():
        raise RuntimeError("model unavailable")

    _, load_service = make_service(store, loader=fail_load)
    with pytest.raises(InferenceCallError) as load_error:
        load_service.detect(audio)

    class BrokenDecoder(FakeBackend):
        def decode(self, stream, sampling_rate):
            raise ValueError("bad audio")

    _, decode_service = make_service(
        store,
        loader=lambda: (BrokenDecoder(), "sha256:model123"),
    )
    with pytest.raises(InferenceCallError) as decode_error:
        decode_service.detect(audio)

    class BrokenDetector(FakeBackend):
        def detect(self, audio, **kwargs):
            raise RuntimeError("inference failed")

    _, detect_service = make_service(
        store,
        loader=lambda: (BrokenDetector(), "sha256:model123"),
    )
    with pytest.raises(InferenceCallError) as detect_error:
        detect_service.detect(audio)

    assert load_error.value.failure.code is InferenceErrorCode.MODEL_UNAVAILABLE
    assert decode_error.value.failure.code is InferenceErrorCode.INVALID_REQUEST
    assert detect_error.value.failure.code is InferenceErrorCode.INFERENCE_FAILED


def test_provider_rejects_invalid_detector_output(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path, namespace="run_123")
    audio = publish_audio(store)

    class InvalidBackend(FakeBackend):
        def detect(self, audio, **kwargs):
            return [{"start": 16000, "end": 8000}]

    _, service = make_service(
        store,
        loader=lambda: (InvalidBackend(), "sha256:model123"),
    )

    with pytest.raises(InferenceCallError) as exc_info:
        service.detect(audio)

    assert exc_info.value.failure.code is InferenceErrorCode.INFERENCE_FAILED


def test_service_rejects_invalid_options_before_loading(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path, namespace="run_123")
    audio = publish_audio(store)
    load_count = 0

    def loader():
        nonlocal load_count
        load_count += 1
        return FakeBackend(), "sha256:model123"

    _, service = make_service(store, loader=loader)

    with pytest.raises(ValueError, match="min_silence_duration_ms"):
        service.detect(audio, min_silence_duration_ms=-1)
    with pytest.raises(ValueError, match="sampling_rate"):
        service.detect(audio, sampling_rate=8000)

    assert load_count == 0


def test_provider_capabilities_health_warmup_and_config_validation(
    tmp_path: Path,
) -> None:
    store = LocalArtifactStore(tmp_path, namespace="run_123")
    load_count = 0

    def loader():
        nonlocal load_count
        load_count += 1
        return FakeBackend(), "sha256:model123"

    provider, _ = make_service(store, loader=loader)
    capabilities = asyncio.run(provider.capabilities())
    health = asyncio.run(provider.health())

    assert capabilities.model_aliases == ("vad.default",)
    assert capabilities.tasks == (InferenceTask.VOICE_ACTIVITY_DETECTION,)
    assert health.status is HealthState.AVAILABLE
    assert load_count == 0

    asyncio.run(provider.warmup())

    assert load_count == 1
    assert provider.is_loaded

    with pytest.raises(ValueError, match="max_artifact_bytes"):
        LocalVADProvider(
            alias="vad.default",
            model_name="silero-vad-v6",
            artifact_store=store,
            max_artifact_bytes=0,
        )
