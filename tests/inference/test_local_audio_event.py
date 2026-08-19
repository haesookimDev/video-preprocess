"""Tests for the ArtifactRef-backed local AudioSet provider."""

import asyncio
import io
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from video_preprocess.domain import HealthState, InferenceErrorCode, InferenceTask
from video_preprocess.inference import AudioEventService, InferenceCallError
from video_preprocess.inference.gateway import InferenceGateway
from video_preprocess.inference.local import (
    AST_AUDIOSET_MAPPING_VERSION,
    LocalAudioEventProvider,
    build_audioset_label_mapping,
)
from video_preprocess.inference.local import audio_event as audio_event_module
from video_preprocess.inference.local.audio_event import _select_auto_device
from video_preprocess.storage import LocalArtifactStore


def audioset_labels() -> dict[int, str]:
    labels = {index: f"label-{index}" for index in range(527)}
    labels.update({
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
    })
    return labels


class FakeModel:
    def __init__(self) -> None:
        self.config = SimpleNamespace(
            id2label=audioset_labels(),
            _commit_hash="commit_ast_123",
        )


def wav_bytes(
    *,
    duration_sec: float = 6.0,
    sampling_rate: int = 16000,
    channels: int = 1,
    sample_width: int = 2,
) -> bytes:
    sample_count = int(duration_sec * sampling_rate)
    samples = (np.sin(
        2 * np.pi * 440 * np.arange(sample_count) / sampling_rate
    ) * 1000).astype("<i2")
    stream = io.BytesIO()
    with wave.open(stream, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(sample_width)
        wav.setframerate(sampling_rate)
        wav.writeframes(samples.tobytes())
    return stream.getvalue()


def publish_audio(
    store: LocalArtifactStore,
    name: str = "audio",
    payload: bytes | None = None,
):
    pending = store.put(
        io.BytesIO(wav_bytes() if payload is None else payload),
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
    classifier,
    decoder=audio_event_module._default_decoder,
    max_batch_size: int = 2,
):
    provider = LocalAudioEventProvider(
        alias="audio_event.default",
        model_name="example/ast",
        artifact_store=store,
        device="auto",
        max_batch_size=max_batch_size,
        loader=loader,
        decoder=decoder,
        device_resolver=lambda requested: (
            "cpu" if requested == "auto" else requested
        ),
        classifier=classifier,
    )
    service = AudioEventService(
        InferenceGateway({"audio_event.default": provider}),
        alias="audio_event.default",
        model_name="example/ast",
        revision=provider.requested_revision,
        batch_size=max_batch_size,
    )
    return provider, service


def test_mapping_validates_ast_label_space_and_covers_taxonomy() -> None:
    mapping = build_audioset_label_mapping(audioset_labels())

    assert mapping[16] == "laughter"
    assert mapping[63] == "applause"
    assert mapping[72] == "animal"
    assert mapping[137] == "music"
    assert mapping[310] == "alarm"
    assert mapping[323] == "siren"
    assert mapping[300] == "vehicle"
    assert mapping[354] == "door"
    assert mapping[426] == "impact"
    assert mapping[513] == "noise"
    assert 0 not in mapping

    invalid = audioset_labels()
    invalid[137] = "Not Music"
    with pytest.raises(ValueError, match="label order"):
        build_audioset_label_mapping(invalid)


def test_provider_classifies_windows_and_reuses_audio_model_and_result(
    tmp_path: Path,
) -> None:
    store = LocalArtifactStore(tmp_path, namespace="run_123")
    audio = publish_audio(store)
    load_count = 0
    classify_count = 0
    batch_sizes = []

    def loader(model_name, revision, device):
        nonlocal load_count
        load_count += 1
        assert (model_name, revision, device) == (
            "example/ast",
            None,
            "cpu",
        )
        return SimpleNamespace(_commit_hash="processor_commit"), FakeModel()

    def classifier(extractor, model, samples, sampling_rate, device):
        nonlocal classify_count
        classify_count += 1
        batch_sizes.append(len(samples))
        assert sampling_rate == 16000
        assert device == "cpu"
        rows = []
        for _ in samples:
            row = [0.0] * 527
            if classify_count == 1:
                row[137] = 0.91
                row[63] = 0.83
            else:
                row[513] = 0.77
            rows.append(row)
        return rows

    provider, service = make_service(
        store,
        loader=loader,
        classifier=classifier,
    )

    first = service.detect(
        audio,
        duration_sec=6.0,
        labels=("music", "applause", "noise"),
        min_confidence=0.5,
        window_sec=3.0,
        hop_sec=2.0,
    )
    second = service.detect(
        audio,
        duration_sec=6.0,
        labels=("music", "applause", "noise"),
        min_confidence=0.5,
        window_sec=3.0,
        hop_sec=2.0,
    )

    assert [event.to_dict() for event in first.events] == [
        {
            "event_id": 1,
            "label": "applause",
            "confidence": 0.83,
            "start_sec": 0.0,
            "end_sec": 5.0,
            "duration_sec": 5.0,
            "source_window_ids": [1, 2],
        },
        {
            "event_id": 2,
            "label": "music",
            "confidence": 0.91,
            "start_sec": 0.0,
            "end_sec": 5.0,
            "duration_sec": 5.0,
            "source_window_ids": [1, 2],
        },
        {
            "event_id": 3,
            "label": "noise",
            "confidence": 0.77,
            "start_sec": 4.0,
            "end_sec": 6.0,
            "duration_sec": 2.0,
            "source_window_ids": [3],
        },
    ]
    assert second.events == first.events
    assert first.model.provider == "local.audio-event"
    assert first.model.revision == "commit_ast_123"
    assert AST_AUDIOSET_MAPPING_VERSION in first.model.runtime
    assert first.usage["batch_sizes"] == [2, 1]
    assert batch_sizes == [2, 1]
    assert classify_count == 2
    assert load_count == 1
    assert provider.is_loaded


def test_provider_decodes_wav_once_across_different_chunks(
    tmp_path: Path,
) -> None:
    store = LocalArtifactStore(tmp_path, namespace="run_123")
    audio = publish_audio(store)
    decode_count = 0

    def decoder(stream, sampling_rate):
        nonlocal decode_count
        decode_count += 1
        return audio_event_module._default_decoder(stream, sampling_rate)

    def classifier(*args):
        samples = args[2]
        return [[0.0] * 527 for _ in samples]

    _, service = make_service(
        store,
        loader=lambda *_: (SimpleNamespace(), FakeModel()),
        classifier=classifier,
        decoder=decoder,
        max_batch_size=1,
    )

    service.detect(
        audio,
        duration_sec=6.0,
        labels=("music",),
        min_confidence=0.5,
        window_sec=3.0,
        hop_sec=2.0,
    )

    assert decode_count == 1


def test_window_end_clamps_only_metadata_rounding_tolerance() -> None:
    decoded = np.zeros(16000, dtype=np.float32)

    clamped = LocalAudioEventProvider._window_samples(
        decoded,
        [{"window_id": 1, "start_sec": 0.5, "end_sec": 1.001}],
        16000,
    )

    assert len(clamped[0]) == 8000
    padded = LocalAudioEventProvider._window_samples(
        decoded,
        [{"window_id": 1, "start_sec": 0.999, "end_sec": 1.0}],
        16000,
    )
    assert len(padded[0]) == 400
    with pytest.raises(ValueError, match="exceeds decoded audio duration"):
        LocalAudioEventProvider._window_samples(
            decoded,
            [{"window_id": 1, "start_sec": 0.5, "end_sec": 1.02}],
            16000,
        )


def test_provider_normalizes_artifact_decode_load_and_inference_failures(
    tmp_path: Path,
) -> None:
    store = LocalArtifactStore(tmp_path, namespace="run_123")
    missing = publish_audio(store, "missing")
    invalid = publish_audio(store, "invalid", b"not-a-wav")
    (tmp_path / "audio" / "missing.wav").unlink()

    def classifier(*args):
        return [[0.0] * 527 for _ in args[2]]

    _, service = make_service(
        store,
        loader=lambda *_: (SimpleNamespace(), FakeModel()),
        classifier=classifier,
    )
    with pytest.raises(InferenceCallError) as missing_error:
        service.detect(missing, duration_sec=1.0, labels=("music",))
    with pytest.raises(InferenceCallError) as decode_error:
        service.detect(invalid, duration_sec=1.0, labels=("music",))

    audio = publish_audio(store, "load")
    _, load_service = make_service(
        store,
        loader=lambda *_: (_ for _ in ()).throw(RuntimeError("unavailable")),
        classifier=classifier,
    )
    with pytest.raises(InferenceCallError) as load_error:
        load_service.detect(audio, duration_sec=1.0, labels=("music",))

    def fail_classifier(*_):
        raise RuntimeError("CPU out of memory")

    _, infer_service = make_service(
        store,
        loader=lambda *_: (SimpleNamespace(), FakeModel()),
        classifier=fail_classifier,
    )
    with pytest.raises(InferenceCallError) as infer_error:
        infer_service.detect(audio, duration_sec=1.0, labels=("music",))

    assert missing_error.value.failure.code is InferenceErrorCode.ARTIFACT_NOT_FOUND
    assert decode_error.value.failure.code is InferenceErrorCode.INVALID_REQUEST
    assert load_error.value.failure.code is InferenceErrorCode.MODEL_UNAVAILABLE
    assert infer_error.value.failure.code is InferenceErrorCode.INFERENCE_FAILED
    assert infer_error.value.failure.details["reason"] == "DEVICE_OUT_OF_MEMORY"


def test_provider_capabilities_health_effective_model_and_warmup_are_lazy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = LocalArtifactStore(tmp_path, namespace="run_123")
    load_count = 0

    def loader(*_):
        nonlocal load_count
        load_count += 1
        return SimpleNamespace(), FakeModel()

    monkeypatch.setattr(
        audio_event_module,
        "resolve_hf_cache_revision",
        lambda *args: "resolved-ast-revision",
    )
    provider, _ = make_service(
        store,
        loader=loader,
        classifier=lambda *args: [],
    )

    capabilities = asyncio.run(provider.capabilities())
    health = asyncio.run(provider.health())
    effective = asyncio.run(provider.effective_model())

    assert capabilities.tasks == (InferenceTask.AUDIO_EVENT_DETECTION,)
    assert capabilities.max_batch_size == 2
    assert AST_AUDIOSET_MAPPING_VERSION in capabilities.features
    assert health.status is HealthState.AVAILABLE
    assert health.details["model_loaded"] is False
    assert health.details["resolved_device"] is None
    assert effective is not None
    assert effective.revision == "resolved-ast-revision"
    assert effective.runtime.endswith(
        f"device=cpu;mapping={AST_AUDIOSET_MAPPING_VERSION}"
    )
    assert load_count == 0

    asyncio.run(provider.warmup())

    assert provider.is_loaded
    assert load_count == 1


def test_default_loader_prefers_complete_local_cache(monkeypatch) -> None:
    calls = []

    class Factory:
        @staticmethod
        def from_pretrained(model_name, **options):
            calls.append(options)
            return SimpleNamespace(
                eval=lambda: None,
                to=lambda device: None,
            )

    import transformers

    monkeypatch.setattr(
        audio_event_module,
        "_has_cached_hf_file",
        lambda *args: True,
    )
    monkeypatch.setattr(transformers, "AutoFeatureExtractor", Factory)
    monkeypatch.setattr(
        transformers,
        "AutoModelForAudioClassification",
        Factory,
    )

    audio_event_module._default_loader("example/ast", None, None)

    assert calls == [
        {"local_files_only": True},
        {"local_files_only": True},
    ]


def test_default_loader_retries_hub_when_local_weights_are_incomplete(
    monkeypatch,
) -> None:
    extractor_calls = []
    model_calls = []

    class ExtractorFactory:
        @staticmethod
        def from_pretrained(model_name, **options):
            extractor_calls.append(options)
            return SimpleNamespace()

    class ModelFactory:
        @staticmethod
        def from_pretrained(model_name, **options):
            model_calls.append(options)
            if options.get("local_files_only"):
                raise OSError("weights missing")
            return SimpleNamespace(eval=lambda: None)

    import transformers

    monkeypatch.setattr(
        audio_event_module,
        "_has_cached_hf_file",
        lambda *args: True,
    )
    monkeypatch.setattr(
        transformers,
        "AutoFeatureExtractor",
        ExtractorFactory,
    )
    monkeypatch.setattr(
        transformers,
        "AutoModelForAudioClassification",
        ModelFactory,
    )

    audio_event_module._default_loader("example/ast", "revision", None)

    assert extractor_calls == [
        {"revision": "revision", "local_files_only": True},
        {"revision": "revision"},
    ]
    assert model_calls == [
        {"revision": "revision", "local_files_only": True},
        {"revision": "revision"},
    ]


@pytest.mark.parametrize(
    ("cuda_available", "mps_available", "expected"),
    [
        (True, True, "cuda"),
        (False, True, "mps"),
        (False, False, "cpu"),
    ],
)
def test_auto_device_selection_has_stable_fallback_order(
    cuda_available: bool,
    mps_available: bool,
    expected: str,
) -> None:
    runtime = SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: cuda_available),
        backends=SimpleNamespace(
            mps=SimpleNamespace(is_available=lambda: mps_available),
        ),
    )

    assert _select_auto_device(runtime) == expected
