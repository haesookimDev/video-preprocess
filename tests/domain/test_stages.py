"""Tests for versioned Stage contracts."""

import json

import pytest

from video_preprocess.domain import (
    ArtifactRef,
    Checksum,
    ContractValidationError,
    ModelExecution,
    ResourceHints,
    StageResult,
    StageSpec,
    StageStatus,
    StageTask,
)


def make_artifact() -> ArtifactRef:
    return ArtifactRef(
        artifact_id="art_audio",
        kind="audio",
        uri="artifact://runs/run_123/04_audio/audio_16k.wav",
        media_type="audio/wav",
        size_bytes=960084,
        checksum=Checksum("sha256", "abc123"),
    )


def test_stage_spec_round_trip_normalizes_sequences() -> None:
    spec = StageSpec(
        name="06_stt",
        stage_version="1.0.0",
        dependencies=["04_audio", "05_vad"],
        required_inputs=["audio", "vad_segments"],
        outputs=["transcript"],
        model_slots=["stt"],
        resource_hints=ResourceHints(
            cpu=2,
            memory_mb=4096,
            gpu_optional=True,
        ),
    )

    payload = json.loads(json.dumps(spec.to_dict()))
    restored = StageSpec.from_dict(payload)

    assert restored == spec
    assert restored.dependencies == ("04_audio", "05_vad")
    assert restored.resource_hints.cpu == 2.0


def test_stage_spec_rejects_self_dependency() -> None:
    with pytest.raises(ContractValidationError) as exc_info:
        StageSpec(
            name="06_stt",
            stage_version="1.0.0",
            dependencies=["06_stt"],
        )

    assert exc_info.value.field == "dependencies"


def test_stage_spec_rejects_duplicate_outputs() -> None:
    with pytest.raises(ContractValidationError) as exc_info:
        StageSpec(
            name="06_stt",
            stage_version="1.0.0",
            outputs=["transcript", "transcript"],
        )

    assert exc_info.value.field == "outputs"


def test_stage_task_round_trip_preserves_explicit_inputs() -> None:
    task = StageTask(
        run_id="run_123",
        stage_run_id="stage_456",
        attempt=1,
        stage="06_stt",
        stage_version="1.0.0",
        inputs={"audio": make_artifact()},
        config={
            "language": "ko",
            "word_timestamps": True,
            "temperatures": (0.0, 0.2),
        },
        model_bindings={"stt": "stt.default"},
        idempotency_key="idem_123",
        trace_id="trace_123",
    )

    payload = json.loads(
        json.dumps(task.to_dict(), ensure_ascii=False, allow_nan=False)
    )
    restored = StageTask.from_dict(payload)

    assert restored == task
    assert restored.config["temperatures"] == [0.0, 0.2]
    assert restored.inputs["audio"].artifact_id == "art_audio"


def test_stage_task_rejects_invalid_attempt() -> None:
    with pytest.raises(ContractValidationError) as exc_info:
        StageTask(
            run_id="run_123",
            stage_run_id="stage_456",
            attempt=0,
            stage="06_stt",
            stage_version="1.0.0",
            inputs={},
            config={},
            model_bindings={},
            idempotency_key="idem_123",
            trace_id="trace_123",
        )

    assert exc_info.value.field == "attempt"


def test_stage_task_rejects_non_artifact_input() -> None:
    with pytest.raises(ContractValidationError) as exc_info:
        StageTask(
            run_id="run_123",
            stage_run_id="stage_456",
            attempt=1,
            stage="06_stt",
            stage_version="1.0.0",
            inputs={"audio": "/tmp/audio.wav"},
            config={},
            model_bindings={},
            idempotency_key="idem_123",
            trace_id="trace_123",
        )

    assert exc_info.value.field == "inputs.audio"


def test_stage_result_round_trip_records_effective_model() -> None:
    result = StageResult(
        run_id="run_123",
        stage_run_id="stage_456",
        attempt=1,
        status=StageStatus.SUCCEEDED,
        outputs={"transcript": make_artifact()},
        metrics={"segment_count": 3, "elapsed_sec": 1.25},
        models=[
            ModelExecution(
                slot="stt",
                provider="local",
                model="faster-whisper",
                revision="base",
                runtime="ctranslate2",
            )
        ],
        warnings=["sample warning"],
    )

    payload = json.loads(json.dumps(result.to_dict(), allow_nan=False))
    restored = StageResult.from_dict(payload)

    assert restored == result
    assert restored.status is StageStatus.SUCCEEDED
    assert restored.models[0].provider == "local"


def test_stage_result_accepts_serialized_status_string() -> None:
    result = StageResult(
        run_id="run_123",
        stage_run_id="stage_456",
        attempt=1,
        status="skipped",
        reason_code="OPTIONAL_CREDENTIAL_MISSING",
        reason="HF token is not configured",
    )

    assert result.status is StageStatus.SKIPPED


def test_stage_result_rejects_unknown_status() -> None:
    with pytest.raises(ContractValidationError) as exc_info:
        StageResult(
            run_id="run_123",
            stage_run_id="stage_456",
            attempt=1,
            status="running",
        )

    assert exc_info.value.field == "status"


def test_stage_result_rejects_non_finite_metric() -> None:
    with pytest.raises(ContractValidationError) as exc_info:
        StageResult(
            run_id="run_123",
            stage_run_id="stage_456",
            attempt=1,
            status=StageStatus.SUCCEEDED,
            metrics={"elapsed_sec": float("nan")},
        )

    assert exc_info.value.field == "metrics.elapsed_sec"

