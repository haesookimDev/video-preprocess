"""Tests for the Engine-backed preprocessing CLI adapter."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import run_pipeline

from pipeline.preflight import CheckResult, PreflightReport
from video_preprocess.domain import RunStatus, StageStatus


def ready_preflight(monkeypatch):
    report = PreflightReport((CheckResult("python", "ok", "ready"),))
    monkeypatch.setattr(run_pipeline, "run_preflight", lambda _: report)


def test_dry_run_prints_cache_aware_exact_plan_without_writes(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    ready_preflight(monkeypatch)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_pipeline.py",
            str(video),
            "--out",
            str(tmp_path / "output"),
            "--stage",
            "10_index",
            "--dry-run",
        ],
    )

    exit_code = run_pipeline.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["stages"] == ["10_index"]
    assert payload["boundary_inputs"] == ["timeline"]
    assert payload["cache_decisions"] == [
        {
            "stage": "10_index",
            "status": "blocked",
            "will_execute": False,
            "reasons": [
                {
                    "code": "REQUIRED_INPUT_UNAVAILABLE",
                    "subject": "timeline",
                    "detail": None,
                }
            ],
        }
    ]
    assert payload["execution_policy"] == {
        "stage_timeout_sec": None,
        "max_stage_attempts": 1,
        "retry_backoff_sec": 0.0,
        "executor_max_concurrency": 1,
    }
    assert not (tmp_path / "output" / "video").exists()


def test_force_dry_run_marks_every_planned_stage(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    ready_preflight(monkeypatch)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_pipeline.py", str(video), "--force", "--dry-run"],
    )

    assert run_pipeline.main() == 0
    payload = json.loads(capsys.readouterr().out)

    assert len(payload["stages"]) == 14
    assert payload["force_stages"] == payload["stages"]
    assert payload["cache_decisions"][0]["status"] == "forced"
    assert payload["cache_decisions"][0]["will_execute"] is True
    assert payload["cache_decisions"][1]["status"] == "blocked"


def test_invalid_selection_is_a_cli_input_error(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    ready_preflight(monkeypatch)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_pipeline.py",
            str(video),
            "--stage",
            "06_stt",
            "--from-stage",
            "06_stt",
            "--dry-run",
        ],
    )

    assert run_pipeline.main() == 2
    assert "cannot be combined" in capsys.readouterr().err


def test_invalid_execution_policy_is_a_cli_input_error(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    ready_preflight(monkeypatch)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_pipeline.py",
            str(video),
            "--max-stage-attempts",
            "0",
            "--dry-run",
        ],
    )

    assert run_pipeline.main() == 2
    assert "max_stage_attempts" in capsys.readouterr().err


def test_invalid_executor_concurrency_is_a_cli_input_error(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    ready_preflight(monkeypatch)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_pipeline.py",
            str(video),
            "--executor-max-concurrency",
            "0",
            "--dry-run",
        ],
    )

    assert run_pipeline.main() == 2
    assert "executor_max_concurrency" in capsys.readouterr().err


def test_caption_tuning_is_reported_by_dry_run(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    ready_preflight(monkeypatch)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_pipeline.py",
            str(video),
            "--stage",
            "10_index",
            "--dry-run",
            "--caption-device",
            "cpu",
            "--caption-batch-size",
            "2",
        ],
    )

    assert run_pipeline.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["local_inference"] == {
        "caption_device": "cpu",
        "caption_batch_size": 2,
        "audio_event_device": "auto",
        "audio_event_batch_size": 8,
        "ocr_command": "tesseract",
        "ocr_batch_size": 4,
    }


def test_invalid_caption_batch_size_is_a_cli_input_error(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    ready_preflight(monkeypatch)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_pipeline.py",
            str(video),
            "--caption-batch-size",
            "0",
            "--dry-run",
        ],
    )

    assert run_pipeline.main() == 2
    assert "caption_batch_size" in capsys.readouterr().err


def test_enabled_audio_events_use_local_provider_without_endpoint(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    ready_preflight(monkeypatch)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_pipeline.py",
            str(video),
            "--stage",
            "01_probe",
            "--audio-event-mode",
            "all",
            "--audio-event-device",
            "cpu",
            "--dry-run",
        ],
    )

    assert run_pipeline.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["inference_deployments"] == {}
    assert payload["local_inference"]["audio_event_device"] == "cpu"


def test_enabled_local_ocr_requires_command(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    ready_preflight(monkeypatch)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(run_pipeline.shutil, "which", lambda _: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_pipeline.py",
            str(video),
            "--ocr-mode",
            "all",
            "--dry-run",
        ],
    )

    assert run_pipeline.main() == 1
    assert "OCR command" in capsys.readouterr().err


def test_remote_ocr_does_not_require_local_command(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    ready_preflight(monkeypatch)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(run_pipeline.shutil, "which", lambda _: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_pipeline.py",
            str(video),
            "--stage",
            "01_probe",
            "--ocr-mode",
            "all",
            "--ocr-model",
            "example/ocr",
            "--ocr-endpoint",
            "https://ocr.example.test",
            "--ocr-artifact-namespace",
            "shared",
            "--dry-run",
        ],
    )

    assert run_pipeline.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["inference_deployments"]["ocr.default"] == {
        "provider": "http",
        "endpoint": "https://ocr.example.test",
        "allowed_artifact_namespaces": ["shared"],
        "request_timeout_sec": 300.0,
    }


def test_invalid_ocr_settings_are_cli_input_errors(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    ready_preflight(monkeypatch)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_pipeline.py",
            str(video),
            "--ocr-min-confidence",
            "2",
            "--dry-run",
        ],
    )

    assert run_pipeline.main() == 2
    assert "ocr_min_confidence" in capsys.readouterr().err


def test_invalid_keyframe_maximum_is_a_cli_input_error(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    ready_preflight(monkeypatch)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_pipeline.py",
            str(video),
            "--keyframes-per-scene",
            "4",
            "--dry-run",
        ],
    )

    assert run_pipeline.main() == 2
    assert "keyframes_per_scene" in capsys.readouterr().err


def test_compatibility_summary_preserves_status_metrics_and_outputs(
    tmp_path: Path,
) -> None:
    result = SimpleNamespace(
        run_id="run-123",
        status=RunStatus.SUCCEEDED,
        stages=(
            SimpleNamespace(
                stage="01_probe",
                task=SimpleNamespace(attempt=1),
                from_cache=False,
                cache_decision=None,
                result=SimpleNamespace(
                    status=StageStatus.SUCCEEDED,
                    metrics={"duration_sec": 10.0},
                    outputs={},
                ),
            ),
        ),
    )

    summary = run_pipeline._write_compatibility_summary(
        result,
        video_path=tmp_path / "video.mp4",
        output_root=tmp_path / "output",
    )

    saved = json.loads(
        (tmp_path / "output" / "run_summary.json").read_text(
            encoding="utf-8"
        )
    )
    assert summary == saved
    assert saved["status"] == "ok"
    assert saved["stages"][0]["status"] == "ok"
    assert saved["stages"][0]["result"] == {"duration_sec": 10.0}


def test_remote_embedding_dry_run_reports_redacted_deployment(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    ready_preflight(monkeypatch)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    monkeypatch.setenv("MODEL_SERVER_TOKEN", "private-token")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_pipeline.py",
            str(video),
            "--stage",
            "10_index",
            "--dry-run",
            "--embedding-endpoint",
            "https://models.example.test",
            "--embedding-token-env",
            "MODEL_SERVER_TOKEN",
        ],
    )

    assert run_pipeline.main() == 0
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert payload["inference_deployments"]["embedding.default"] == {
        "provider": "http",
        "endpoint": "https://models.example.test",
        "allowed_artifact_namespaces": [],
        "request_timeout_sec": 300.0,
    }
    assert "private-token" not in output
    assert "MODEL_SERVER_TOKEN" not in output


def test_remote_embedding_requires_configured_token_environment(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    ready_preflight(monkeypatch)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    monkeypatch.delenv("MISSING_MODEL_TOKEN", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_pipeline.py",
            str(video),
            "--dry-run",
            "--embedding-endpoint",
            "https://models.example.test",
            "--embedding-token-env",
            "MISSING_MODEL_TOKEN",
        ],
    )

    assert run_pipeline.main() == 2
    error = capsys.readouterr().err
    assert "environment variable is empty" in error
    assert "private-token" not in error
