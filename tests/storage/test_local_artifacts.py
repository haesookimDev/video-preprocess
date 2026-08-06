"""Tests for the filesystem-backed Artifact Store."""

import io
import shutil
from pathlib import Path

import pytest

from video_preprocess.domain import ArtifactRef, Checksum
from video_preprocess.storage import (
    ArtifactIntegrityError,
    InvalidArtifactPathError,
    InvalidArtifactURIError,
    LegacyOutputAdapter,
    LocalArtifactStore,
    PendingArtifactError,
)


FIXTURES = Path(__file__).parents[1] / "fixtures" / "legacy_v1"


def test_put_then_publish_is_atomic_and_verifiable(tmp_path: Path) -> None:
    root = tmp_path / "output" / "sample"
    store = LocalArtifactStore(root, namespace="run_123")
    payload = "안녕하세요".encode()

    pending = store.put(
        io.BytesIO(payload),
        artifact_id="art_transcript",
        relative_path="06_stt/transcript.json",
        kind="json",
        media_type="application/json",
        metadata={"stage": "06_stt"},
    )

    target = root / "06_stt" / "transcript.json"
    assert not target.exists()
    assert pending.checksum.algorithm == "sha256"

    artifact = store.publish(pending)

    assert target.read_bytes() == payload
    assert artifact.uri == "artifact://run_123/06_stt/transcript.json"
    assert artifact.size_bytes == len(payload)
    assert store.exists(artifact)
    assert store.verify(artifact).ok
    with store.open(artifact) as handle:
        assert handle.read() == payload


def test_publish_failure_preserves_previous_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "sample"
    target = root / "01_probe" / "metadata.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old")
    store = LocalArtifactStore(root, namespace="run_123")
    pending = store.put(
        io.BytesIO(b"new"),
        artifact_id="art_metadata",
        relative_path="01_probe/metadata.json",
        kind="json",
        media_type="application/json",
    )

    def fail_replace(source: object, destination: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(
        "video_preprocess.storage.local_artifacts.os.replace",
        fail_replace,
    )

    with pytest.raises(OSError, match="simulated"):
        store.publish(pending)

    assert target.read_bytes() == b"old"


def test_discard_removes_unpublished_artifact(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path, namespace="run_123")
    pending = store.put(
        io.BytesIO(b"temporary"),
        artifact_id="art_temp",
        relative_path="01_probe/temp.json",
        kind="json",
        media_type="application/json",
    )

    store.discard(pending)

    with pytest.raises(PendingArtifactError):
        store.publish(pending)


def test_verify_detects_tampered_artifact(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path, namespace="run_123")
    pending = store.put(
        io.BytesIO(b"original"),
        artifact_id="art_audio",
        relative_path="04_audio/audio.wav",
        kind="audio",
        media_type="audio/wav",
    )
    artifact = store.publish(pending)
    (tmp_path / "04_audio" / "audio.wav").write_bytes(b"tampered")

    verification = store.verify(artifact)

    assert verification.exists
    assert not verification.ok
    assert not verification.checksum_matches


@pytest.mark.parametrize(
    "relative_path",
    ["/absolute/file.json", "../escape.json", "a/../../escape", "_manifests/x"],
)
def test_put_rejects_unsafe_or_reserved_paths(
    tmp_path: Path,
    relative_path: str,
) -> None:
    store = LocalArtifactStore(tmp_path, namespace="run_123")

    with pytest.raises(InvalidArtifactPathError):
        store.put(
            io.BytesIO(b"data"),
            artifact_id="art_test",
            relative_path=relative_path,
            kind="json",
            media_type="application/json",
        )


def test_store_rejects_artifact_from_another_namespace(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path, namespace="run_123")
    foreign = ArtifactRef(
        artifact_id="art_foreign",
        kind="json",
        uri="artifact://run_999/01_probe/metadata.json",
        media_type="application/json",
        size_bytes=1,
        checksum=Checksum("sha256", "00"),
    )

    with pytest.raises(InvalidArtifactURIError):
        store.exists(foreign)


def test_materialize_copies_verified_bytes(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "store", namespace="run_123")
    pending = store.put(
        io.BytesIO(b"audio bytes"),
        artifact_id="art_audio",
        relative_path="04_audio/audio.wav",
        kind="audio",
        media_type="audio/wav",
    )
    artifact = store.publish(pending)

    materialized = store.materialize(artifact, tmp_path / "workspace")

    assert materialized == tmp_path / "workspace" / "audio.wav"
    assert materialized.read_bytes() == b"audio bytes"


def test_materialize_does_not_publish_tampered_bytes(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "store", namespace="run_123")
    pending = store.put(
        io.BytesIO(b"original"),
        artifact_id="art_audio",
        relative_path="04_audio/audio.wav",
        kind="audio",
        media_type="audio/wav",
    )
    artifact = store.publish(pending)
    (tmp_path / "store" / "04_audio" / "audio.wav").write_bytes(b"changed")

    with pytest.raises(ArtifactIntegrityError):
        store.materialize(artifact, tmp_path / "workspace")

    assert not (tmp_path / "workspace" / "audio.wav").exists()


def test_artifact_uri_percent_encodes_relative_path(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path, namespace="run_123")
    pending = store.put(
        io.BytesIO(b"caption"),
        artifact_id="art_caption",
        relative_path="08_captions/장면 1.json",
        kind="json",
        media_type="application/json",
    )

    artifact = store.publish(pending)

    assert "%EC%9E%A5%EB%A9%B4%201.json" in artifact.uri
    assert store.verify(artifact).ok


def test_register_existing_reads_legacy_output_without_rewrite(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sample"
    target = root / "01_probe" / "metadata.json"
    target.parent.mkdir(parents=True)
    shutil.copyfile(FIXTURES / "metadata.json", target)
    original = target.read_bytes()
    store = LocalArtifactStore(root, namespace="sample")

    legacy = LegacyOutputAdapter(store)
    artifact = legacy.register_json(
        "01_probe/metadata.json",
        artifact_id="legacy_metadata",
    )

    assert target.read_bytes() == original
    assert store.verify(artifact).ok
    assert artifact.metadata["legacy_schema"] == "v1"
    metadata = legacy.load_json(artifact)
    assert isinstance(metadata, dict)
    assert metadata["summary"]["duration_sec"] == 30.0
