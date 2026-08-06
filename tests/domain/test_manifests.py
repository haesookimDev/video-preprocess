"""Tests for versioned run and Stage manifests."""

import json

import pytest

from video_preprocess.domain import (
    ArtifactRef,
    Checksum,
    ContractValidationError,
    RunManifest,
    RunStatus,
    StageAttemptRef,
    StageManifest,
    StageResult,
    StageStatus,
    StageTask,
)


def make_artifact() -> ArtifactRef:
    return ArtifactRef(
        artifact_id="art_transcript",
        kind="json",
        uri="artifact://run_123/06_stt/transcript.json",
        media_type="application/json",
        size_bytes=42,
        checksum=Checksum("sha256", "abc123"),
    )


def make_task() -> StageTask:
    return StageTask(
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


def make_stage_manifest() -> StageManifest:
    task = make_task()
    result = StageResult(
        run_id=task.run_id,
        stage_run_id=task.stage_run_id,
        attempt=task.attempt,
        status=StageStatus.SUCCEEDED,
        outputs={"transcript": make_artifact()},
    )
    return StageManifest(
        task=task,
        result=result,
        started_at="2026-08-06T12:00:00+09:00",
        completed_at="2026-08-06T12:00:05+09:00",
        cache_key="cache_123",
    )


def test_stage_manifest_round_trip_preserves_task_and_result() -> None:
    manifest = make_stage_manifest()

    payload = json.loads(json.dumps(manifest.to_dict(), allow_nan=False))
    restored = StageManifest.from_dict(payload)

    assert restored == manifest
    assert restored.reference == StageAttemptRef("stage_456", 1)


def test_stage_manifest_rejects_mismatched_result_identity() -> None:
    task = make_task()
    result = StageResult(
        run_id=task.run_id,
        stage_run_id="another_stage_run",
        attempt=task.attempt,
        status=StageStatus.SUCCEEDED,
    )

    with pytest.raises(ContractValidationError) as exc_info:
        StageManifest(
            task=task,
            result=result,
            started_at="2026-08-06T12:00:00Z",
            completed_at="2026-08-06T12:00:01Z",
        )

    assert exc_info.value.field == "result"


def test_run_manifest_round_trip_tracks_stage_attempts() -> None:
    stage = make_stage_manifest()
    manifest = RunManifest(
        run_id="run_123",
        status=RunStatus.SUCCEEDED,
        started_at="2026-08-06T12:00:00Z",
        updated_at="2026-08-06T12:01:00Z",
        completed_at="2026-08-06T12:01:00Z",
        input_artifacts={"video": make_artifact()},
        config={"language": "ko"},
        model_bindings={"stt": "stt.default"},
        stages=[stage.reference],
    )

    restored = RunManifest.from_dict(
        json.loads(json.dumps(manifest.to_dict(), allow_nan=False))
    )

    assert restored == manifest
    assert restored.status is RunStatus.SUCCEEDED


def test_terminal_run_requires_completed_timestamp() -> None:
    with pytest.raises(ContractValidationError) as exc_info:
        RunManifest(
            run_id="run_123",
            status=RunStatus.FAILED,
            started_at="2026-08-06T12:00:00Z",
            updated_at="2026-08-06T12:01:00Z",
        )

    assert exc_info.value.field == "completed_at"


def test_manifest_timestamps_require_timezone() -> None:
    with pytest.raises(ContractValidationError) as exc_info:
        RunManifest(
            run_id="run_123",
            status=RunStatus.RUNNING,
            started_at="2026-08-06T12:00:00",
            updated_at="2026-08-06T12:01:00Z",
        )

    assert exc_info.value.field == "started_at"

