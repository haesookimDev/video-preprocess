"""Tests for the ArtifactRef-backed local STT provider."""

import asyncio
import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from video_preprocess.domain import (
    HealthState,
    InferenceErrorCode,
    InferenceTask,
)
from video_preprocess.inference import InferenceCallError, STTService
from video_preprocess.inference.gateway import InferenceGateway
from video_preprocess.inference.local import LocalSTTProvider
from video_preprocess.storage import LocalArtifactStore


class FakeModel:
    def __init__(self) -> None:
        self.transcribe_count = 0
        self.audio_lengths = []

    def transcribe(self, audio, *, language, beam_size):
        self.transcribe_count += 1
        self.audio_lengths.append(len(audio))
        assert language is None
        assert beam_size == 5
        segment = SimpleNamespace(
            start=0.1,
            end=0.5,
            text=f" chunk {self.transcribe_count} ",
            avg_logprob=-0.12345,
            no_speech_prob=0.01234,
        )
        info = SimpleNamespace(
            language="ko",
            language_probability=0.9876,
        )
        return [segment], info


class FakeDecoder:
    def __init__(self, sample_count: int = 48000) -> None:
        self.sample_count = sample_count
        self.call_count = 0

    def __call__(self, stream, sampling_rate):
        self.call_count += 1
        assert stream.read() == b"audio"
        assert sampling_rate == 16000
        return list(range(self.sample_count))


def publish_audio(store: LocalArtifactStore, name: str = "audio"):
    pending = store.put(
        io.BytesIO(b"audio"),
        artifact_id=f"art_{name}",
        relative_path=f"audio/{name}.wav",
        kind="audio",
        media_type="audio/wav",
    )
    return store.publish(pending)


def make_service(
    store: LocalArtifactStore,
    *,
    loader,
    decoder,
    max_batch_size: int = 256,
):
    provider = LocalSTTProvider(
        alias="stt.default",
        model_name="base",
        artifact_store=store,
        revision=None,
        device="auto",
        compute_type="int8",
        max_batch_size=max_batch_size,
        loader=loader,
        decoder=decoder,
    )
    service = STTService(
        InferenceGateway({"stt.default": provider}),
        alias="stt.default",
        model_name="base",
        revision=provider.requested_revision,
    )
    return provider, service


def chunks():
    return [
        {"start_sec": 0.5, "end_sec": 1.0, "source_ids": [1]},
        {"start_sec": 1.5, "end_sec": 2.0, "source_ids": [2, 3]},
    ]


def test_provider_transcribes_chunks_and_reuses_model_and_result(
    tmp_path: Path,
) -> None:
    store = LocalArtifactStore(tmp_path, namespace="run_123")
    audio = publish_audio(store)
    model = FakeModel()
    decoder = FakeDecoder()
    load_count = 0

    def loader(model_name, revision, device, compute_type):
        nonlocal load_count
        load_count += 1
        assert (model_name, revision, device, compute_type) == (
            "base",
            None,
            "auto",
            "int8",
        )
        return model, "commit_stt_123"

    provider, service = make_service(
        store,
        loader=loader,
        decoder=decoder,
    )

    first = service.transcribe(audio, chunks())
    second = service.transcribe(audio, chunks())

    assert [segment.to_dict() for segment in first.segments] == [
        {
            "start_sec": 0.6,
            "end_sec": 1.0,
            "text": "chunk 1",
            "avg_logprob": -0.1235,
            "no_speech_prob": 0.0123,
            "vad_source_ids": [1],
        },
        {
            "start_sec": 1.6,
            "end_sec": 2.0,
            "text": "chunk 2",
            "avg_logprob": -0.1235,
            "no_speech_prob": 0.0123,
            "vad_source_ids": [2, 3],
        },
    ]
    assert second.segments == first.segments
    assert first.language == "ko"
    assert first.language_probability == pytest.approx(0.9876)
    assert first.model.provider == "local.stt"
    assert first.model.revision == "commit_stt_123"
    assert first.usage["chunk_count"] == 2
    assert model.audio_lengths == [8000, 8000]
    assert model.transcribe_count == 2
    assert decoder.call_count == 1
    assert load_count == 1
    assert provider.is_loaded


def test_provider_reuses_model_for_different_request(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path, namespace="run_123")
    audio = publish_audio(store)
    model = FakeModel()
    decoder = FakeDecoder()
    provider, service = make_service(
        store,
        loader=lambda *_: (model, "commit_stt_123"),
        decoder=decoder,
    )

    service.transcribe(audio, [chunks()[0]])
    service.transcribe(audio, [chunks()[1]])

    assert provider.is_loaded
    assert model.transcribe_count == 2
    assert decoder.call_count == 2


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
        loader=lambda *_: (FakeModel(), "commit_stt_123"),
        decoder=FakeDecoder(),
    )

    with pytest.raises(InferenceCallError) as missing_error:
        service.transcribe(missing, [chunks()[0]])
    with pytest.raises(InferenceCallError) as corrupt_error:
        service.transcribe(corrupt, [chunks()[0]])

    assert (
        missing_error.value.failure.code
        is InferenceErrorCode.ARTIFACT_NOT_FOUND
    )
    assert (
        corrupt_error.value.failure.code
        is InferenceErrorCode.ARTIFACT_INTEGRITY_ERROR
    )


def test_provider_normalizes_decode_load_and_inference_failures(
    tmp_path: Path,
) -> None:
    store = LocalArtifactStore(tmp_path, namespace="run_123")
    audio = publish_audio(store)

    def fail_decode(*_):
        raise ValueError("bad audio")

    _, decode_service = make_service(
        store,
        loader=lambda *_: (FakeModel(), "commit_stt_123"),
        decoder=fail_decode,
    )
    with pytest.raises(InferenceCallError) as decode_error:
        decode_service.transcribe(audio, [chunks()[0]])

    def fail_load(*_):
        raise RuntimeError("model unavailable")

    _, load_service = make_service(
        store,
        loader=fail_load,
        decoder=FakeDecoder(),
    )
    with pytest.raises(InferenceCallError) as load_error:
        load_service.transcribe(audio, [chunks()[0]])

    class BrokenModel(FakeModel):
        def transcribe(self, audio, *, language, beam_size):
            raise RuntimeError("inference failed")

    _, infer_service = make_service(
        store,
        loader=lambda *_: (BrokenModel(), "commit_stt_123"),
        decoder=FakeDecoder(),
    )
    with pytest.raises(InferenceCallError) as infer_error:
        infer_service.transcribe(audio, [chunks()[0]])

    assert decode_error.value.failure.code is InferenceErrorCode.INVALID_REQUEST
    assert load_error.value.failure.code is InferenceErrorCode.MODEL_UNAVAILABLE
    assert infer_error.value.failure.code is InferenceErrorCode.INFERENCE_FAILED


def test_provider_rejects_chunk_outside_decoded_audio(
    tmp_path: Path,
) -> None:
    store = LocalArtifactStore(tmp_path, namespace="run_123")
    audio = publish_audio(store)
    load_count = 0

    def loader(*_):
        nonlocal load_count
        load_count += 1
        return FakeModel(), "commit_stt_123"

    _, service = make_service(
        store,
        loader=loader,
        decoder=FakeDecoder(sample_count=100),
    )

    with pytest.raises(InferenceCallError) as exc_info:
        service.transcribe(audio, [chunks()[0]])

    assert exc_info.value.failure.code is InferenceErrorCode.INVALID_REQUEST
    assert load_count == 0


def test_gateway_rejects_chunk_batch_before_loading_model(
    tmp_path: Path,
) -> None:
    store = LocalArtifactStore(tmp_path, namespace="run_123")
    audio = publish_audio(store)
    load_count = 0

    def loader(*_):
        nonlocal load_count
        load_count += 1
        return FakeModel(), "commit_stt_123"

    _, service = make_service(
        store,
        loader=loader,
        decoder=FakeDecoder(),
        max_batch_size=1,
    )

    with pytest.raises(InferenceCallError) as exc_info:
        service.transcribe(audio, chunks())

    assert (
        exc_info.value.failure.code
        is InferenceErrorCode.UNSUPPORTED_CAPABILITY
    )
    assert load_count == 0


def test_provider_capabilities_health_warmup_and_config_validation(
    tmp_path: Path,
) -> None:
    store = LocalArtifactStore(tmp_path, namespace="run_123")
    load_count = 0

    def loader(*_):
        nonlocal load_count
        load_count += 1
        return FakeModel(), "commit_stt_123"

    provider, _ = make_service(
        store,
        loader=loader,
        decoder=FakeDecoder(),
    )

    capabilities = asyncio.run(provider.capabilities())
    health = asyncio.run(provider.health())

    assert capabilities.model_aliases == ("stt.default",)
    assert capabilities.tasks == (InferenceTask.SPEECH_TO_TEXT,)
    assert health.status is HealthState.AVAILABLE
    assert health.details["device"] == "auto"
    assert health.details["compute_type"] == "int8"
    assert load_count == 0

    asyncio.run(provider.warmup())

    assert load_count == 1
    assert provider.is_loaded

    with pytest.raises(ValueError, match="device"):
        LocalSTTProvider(
            alias="stt.default",
            model_name="base",
            artifact_store=store,
            device="",
        )
