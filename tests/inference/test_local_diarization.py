"""Tests for the ArtifactRef-backed local diarization provider."""

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
from video_preprocess.inference import DiarizationService, InferenceCallError
from video_preprocess.inference.gateway import InferenceGateway
from video_preprocess.inference.local import LocalDiarizationProvider
from video_preprocess.inference.local.diarization import _snapshot_revision
from video_preprocess.storage import LocalArtifactStore


class FakeAnnotation:
    def itertracks(self, *, yield_label):
        assert yield_label is True
        yield SimpleNamespace(start=0.1254, end=1.8764), None, "SPEAKER_01"
        yield SimpleNamespace(start=1.5, end=2.25), None, "SPEAKER_00"


class FakePipeline:
    def __init__(self) -> None:
        self.call_count = 0
        self.paths = []

    def __call__(self, file):
        self.call_count += 1
        path = Path(file)
        assert path.read_bytes() == b"audio"
        self.paths.append(path)
        return SimpleNamespace(speaker_diarization=FakeAnnotation())


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
    token="hf_test_token",
):
    provider = LocalDiarizationProvider(
        alias="diarization.default",
        model_name="pyannote/test-diarization",
        artifact_store=store,
        token=token,
        loader=loader,
    )
    service = DiarizationService(
        InferenceGateway({"diarization.default": provider}),
        alias="diarization.default",
        model_name="pyannote/test-diarization",
        revision=provider.requested_revision,
    )
    return provider, service


def test_snapshot_revision_keeps_hub_commit_before_symlink_resolution() -> None:
    path = (
        "/cache/models--pyannote--test/snapshots/"
        "3533c8cf8e369892e6b79ff1bf80f7b0286a54ee/config.yaml"
    )

    assert _snapshot_revision(path, "default") == (
        "3533c8cf8e369892e6b79ff1bf80f7b0286a54ee"
    )


def test_provider_returns_turns_and_reuses_pipeline_and_result(
    tmp_path: Path,
) -> None:
    store = LocalArtifactStore(tmp_path, namespace="run_123")
    audio = publish_audio(store)
    pipeline = FakePipeline()
    load_count = 0

    def loader(model_name, revision, token, device):
        nonlocal load_count
        load_count += 1
        assert (model_name, revision, token, device) == (
            "pyannote/test-diarization",
            None,
            "hf_test_token",
            None,
        )
        return pipeline, "commit_diarization_123"

    provider, service = make_service(store, loader=loader)

    first = service.diarize(audio)
    second = service.diarize(audio)

    assert first.speakers == ("SPEAKER_00", "SPEAKER_01")
    assert [turn.to_dict() for turn in first.turns] == [
        {
            "turn_id": 1,
            "start_sec": 0.125,
            "end_sec": 1.876,
            "speaker": "SPEAKER_01",
        },
        {
            "turn_id": 2,
            "start_sec": 1.5,
            "end_sec": 2.25,
            "speaker": "SPEAKER_00",
        },
    ]
    assert second.turns == first.turns
    assert first.model.provider == "local.diarization"
    assert first.model.revision == "commit_diarization_123"
    assert first.usage == {"speaker_count": 2, "turn_count": 2}
    assert load_count == 1
    assert pipeline.call_count == 1
    assert provider.is_loaded
    assert all(not path.exists() for path in pipeline.paths)


def test_provider_reuses_pipeline_for_different_audio(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path, namespace="run_123")
    first_audio = publish_audio(store, "first")
    second_audio = publish_audio(store, "second")
    pipeline = FakePipeline()
    provider, service = make_service(
        store,
        loader=lambda *_: (pipeline, "commit_diarization_123"),
    )

    service.diarize(first_audio)
    service.diarize(second_audio)

    assert provider.is_loaded
    assert pipeline.call_count == 2


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
        loader=lambda *_: (FakePipeline(), "commit_diarization_123"),
    )

    with pytest.raises(InferenceCallError) as missing_error:
        service.diarize(missing)
    with pytest.raises(InferenceCallError) as corrupt_error:
        service.diarize(corrupt)

    assert missing_error.value.failure.code is InferenceErrorCode.ARTIFACT_NOT_FOUND
    assert (
        corrupt_error.value.failure.code
        is InferenceErrorCode.ARTIFACT_INTEGRITY_ERROR
    )


def test_provider_normalizes_credential_and_load_failures(
    tmp_path: Path,
) -> None:
    store = LocalArtifactStore(tmp_path, namespace="run_123")
    audio = publish_audio(store)
    load_count = 0

    def should_not_load(*_):
        nonlocal load_count
        load_count += 1
        return FakePipeline(), "revision"

    _, missing_service = make_service(
        store,
        loader=should_not_load,
        token=None,
    )
    with pytest.raises(InferenceCallError) as missing_error:
        missing_service.diarize(audio)

    class UnauthorizedError(Exception):
        def __init__(self):
            self.response = SimpleNamespace(status_code=401)

    class GatedRepoError(Exception):
        def __init__(self):
            self.response = SimpleNamespace(status_code=403)

    _, auth_service = make_service(
        store,
        loader=lambda *_: (_ for _ in ()).throw(UnauthorizedError()),
    )
    _, gated_service = make_service(
        store,
        loader=lambda *_: (_ for _ in ()).throw(GatedRepoError()),
    )
    _, unavailable_service = make_service(
        store,
        loader=lambda *_: (_ for _ in ()).throw(RuntimeError("offline")),
    )

    with pytest.raises(InferenceCallError) as auth_error:
        auth_service.diarize(audio)
    with pytest.raises(InferenceCallError) as gated_error:
        gated_service.diarize(audio)
    with pytest.raises(InferenceCallError) as unavailable_error:
        unavailable_service.diarize(audio)

    assert load_count == 0
    assert missing_error.value.failure.code is InferenceErrorCode.AUTHENTICATION_FAILED
    assert missing_error.value.failure.details["reason"] == "CREDENTIAL_MISSING"
    assert auth_error.value.failure.code is InferenceErrorCode.AUTHENTICATION_FAILED
    assert gated_error.value.failure.code is InferenceErrorCode.MODEL_ACCESS_DENIED
    assert unavailable_error.value.failure.code is InferenceErrorCode.MODEL_UNAVAILABLE


def test_provider_normalizes_pipeline_failure(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path, namespace="run_123")
    audio = publish_audio(store)

    class BrokenPipeline:
        def __call__(self, file):
            raise RuntimeError("inference failed")

    _, service = make_service(
        store,
        loader=lambda *_: (BrokenPipeline(), "revision"),
    )

    with pytest.raises(InferenceCallError) as exc_info:
        service.diarize(audio)

    assert exc_info.value.failure.code is InferenceErrorCode.INFERENCE_FAILED


def test_provider_capabilities_health_warmup_and_config_validation(
    tmp_path: Path,
) -> None:
    store = LocalArtifactStore(tmp_path, namespace="run_123")
    load_count = 0

    def loader(*_):
        nonlocal load_count
        load_count += 1
        return FakePipeline(), "commit_diarization_123"

    provider, _ = make_service(store, loader=loader)
    capabilities = asyncio.run(provider.capabilities())
    health = asyncio.run(provider.health())

    assert capabilities.model_aliases == ("diarization.default",)
    assert capabilities.tasks == (InferenceTask.SPEAKER_DIARIZATION,)
    assert health.status is HealthState.AVAILABLE
    assert health.details["device"] == "model_default"
    assert load_count == 0

    asyncio.run(provider.warmup())

    assert load_count == 1
    assert provider.is_loaded

    missing_provider, _ = make_service(
        store,
        loader=loader,
        token=None,
    )
    assert asyncio.run(missing_provider.health()).status is HealthState.UNAVAILABLE

    with pytest.raises(ValueError, match="token"):
        LocalDiarizationProvider(
            alias="diarization.default",
            model_name="pyannote/test-diarization",
            artifact_store=store,
            token="",
        )
