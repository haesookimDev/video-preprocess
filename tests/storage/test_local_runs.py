"""Tests for atomic local run and Stage manifests."""

import io
from pathlib import Path

import pytest

from video_preprocess.domain import (
    ArtifactRef,
    Checksum,
    RunManifest,
    RunStatus,
    StageAttemptRef,
    StageManifest,
    StageResult,
    StageStatus,
    StageTask,
)
from video_preprocess.storage import (
    ArtifactIntegrityError,
    IncompleteRunError,
    LocalArtifactStore,
    LocalRunStore,
    StorageError,
)


def publish_artifact(
    store: LocalArtifactStore,
    relative_path: str = "06_stt/transcript.json",
) -> ArtifactRef:
    pending = store.put(
        io.BytesIO(b'{"segments": []}'),
        artifact_id="art_transcript",
        relative_path=relative_path,
        kind="json",
        media_type="application/json",
    )
    return store.publish(pending)


def make_stage_manifest(
    artifact: ArtifactRef,
    *,
    status: StageStatus = StageStatus.SUCCEEDED,
    cache_key: str | None = None,
) -> StageManifest:
    task = StageTask(
        run_id="run_123",
        stage_run_id="stage_456",
        attempt=1,
        stage="06_stt",
        stage_version="1.0.0",
        inputs={},
        config={"language": "ko"},
        model_bindings={"stt": "stt.default"},
        idempotency_key="idem_123",
        trace_id="trace_123",
    )
    result = StageResult(
        run_id=task.run_id,
        stage_run_id=task.stage_run_id,
        attempt=task.attempt,
        status=status,
        outputs={"transcript": artifact},
    )
    return StageManifest(
        task=task,
        result=result,
        started_at="2026-08-06T12:00:00Z",
        completed_at="2026-08-06T12:00:05Z",
        cache_key=cache_key,
    )


def make_run_manifest(stage: StageAttemptRef) -> RunManifest:
    return RunManifest(
        run_id="run_123",
        status=RunStatus.SUCCEEDED,
        started_at="2026-08-06T12:00:00Z",
        updated_at="2026-08-06T12:01:00Z",
        completed_at="2026-08-06T12:01:00Z",
        stages=[stage],
    )


def test_stage_and_run_manifests_round_trip_after_artifact_publish(
    tmp_path: Path,
) -> None:
    artifacts = LocalArtifactStore(tmp_path, namespace="run_123")
    runs = LocalRunStore(tmp_path, artifacts)
    stage = make_stage_manifest(publish_artifact(artifacts))

    runs.save_stage(stage)
    runs.save_run(make_run_manifest(stage.reference))

    assert runs.load_stage("run_123", stage.reference) == stage
    assert runs.is_stage_complete("run_123", stage.reference)
    assert runs.load_run("run_123") == make_run_manifest(stage.reference)


def test_cache_index_returns_successful_manifest_by_content_key(
    tmp_path: Path,
) -> None:
    artifacts = LocalArtifactStore(tmp_path, namespace="run_123")
    runs = LocalRunStore(tmp_path, artifacts)
    stage = make_stage_manifest(
        publish_artifact(artifacts),
        cache_key="stage-cache-v1:content",
    )

    runs.save_stage(stage)

    assert runs.find_stages_by_cache_key(stage.cache_key) == (stage,)
    assert runs.find_stages_by_cache_key("stage-cache-v1:missing") == ()


def test_stage_manifest_is_not_written_for_missing_output(
    tmp_path: Path,
) -> None:
    artifacts = LocalArtifactStore(tmp_path, namespace="run_123")
    runs = LocalRunStore(tmp_path, artifacts)
    missing = ArtifactRef(
        artifact_id="missing",
        kind="json",
        uri="artifact://run_123/06_stt/missing.json",
        media_type="application/json",
        size_bytes=1,
        checksum=Checksum("sha256", "00"),
    )
    stage = make_stage_manifest(missing)

    with pytest.raises(ArtifactIntegrityError):
        runs.save_stage(stage)

    assert runs.load_stage("run_123", stage.reference) is None


def test_deleted_output_invalidates_completed_stage(tmp_path: Path) -> None:
    artifacts = LocalArtifactStore(tmp_path, namespace="run_123")
    runs = LocalRunStore(tmp_path, artifacts)
    stage = make_stage_manifest(publish_artifact(artifacts))
    runs.save_stage(stage)

    (tmp_path / "06_stt" / "transcript.json").unlink()

    assert not runs.is_stage_complete("run_123", stage.reference)


def test_succeeded_run_rejects_missing_stage_manifest(tmp_path: Path) -> None:
    artifacts = LocalArtifactStore(tmp_path, namespace="run_123")
    runs = LocalRunStore(tmp_path, artifacts)
    stage = StageAttemptRef("stage_missing", 1)

    with pytest.raises(IncompleteRunError):
        runs.save_run(make_run_manifest(stage))

    assert runs.load_run("run_123") is None


def test_failed_stage_is_not_complete(tmp_path: Path) -> None:
    artifacts = LocalArtifactStore(tmp_path, namespace="run_123")
    runs = LocalRunStore(tmp_path, artifacts)
    artifact = publish_artifact(artifacts)
    stage = make_stage_manifest(artifact, status=StageStatus.FAILED)
    runs.save_stage(stage)

    assert not runs.is_stage_complete("run_123", stage.reference)


def test_atomic_run_update_preserves_previous_manifest_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = LocalArtifactStore(tmp_path, namespace="run_123")
    runs = LocalRunStore(tmp_path, artifacts)
    original = RunManifest(
        run_id="run_123",
        status=RunStatus.RUNNING,
        started_at="2026-08-06T12:00:00Z",
        updated_at="2026-08-06T12:00:00Z",
    )
    updated = RunManifest(
        run_id="run_123",
        status=RunStatus.RUNNING,
        started_at="2026-08-06T12:00:00Z",
        updated_at="2026-08-06T12:01:00Z",
    )
    runs.save_run(original)

    def fail_replace(source: object, destination: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(
        "video_preprocess.storage._atomic.os.replace",
        fail_replace,
    )

    with pytest.raises(OSError, match="simulated"):
        runs.save_run(updated)

    assert runs.load_run("run_123") == original
    run_directory = tmp_path / "_manifests" / "id-run_123"
    assert list(run_directory.glob(".run.json.*.tmp")) == []


def test_read_only_run_store_does_not_create_layout(tmp_path: Path) -> None:
    root = tmp_path / "missing"
    artifacts = LocalArtifactStore(
        root,
        namespace="preview",
        read_only=True,
    )
    store = LocalRunStore(root, artifacts, read_only=True)

    assert store.load_run("missing") is None
    assert not root.exists()
    with pytest.raises(StorageError, match="read-only"):
        store.save_run(
            RunManifest(
                run_id="run-preview",
                status=RunStatus.RUNNING,
                started_at="2026-08-12T00:00:00Z",
                updated_at="2026-08-12T00:00:00Z",
            )
        )
