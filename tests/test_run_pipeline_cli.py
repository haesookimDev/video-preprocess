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

    assert len(payload["stages"]) == 11
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
