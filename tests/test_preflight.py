"""Unit tests for network-free runtime preflight checks."""

import sys
from pathlib import Path

import run_pipeline
from pipeline.preflight import (
    CheckResult,
    PreflightReport,
    check_command,
    check_module,
    check_python,
    check_sqlite_fts5,
    format_report,
    load_hf_token,
)


def test_python_check_enforces_minimum_version() -> None:
    assert check_python((3, 10, 0)).status == "ok"

    result = check_python((3, 9, 9))

    assert result.status == "error"
    assert result.remediation is not None


def test_command_check_reports_missing_binary() -> None:
    result = check_command("ffmpeg", which=lambda _: None)

    assert result.status == "error"
    assert "PATH" in result.detail


def test_optional_ocr_command_reports_warning() -> None:
    result = check_command(
        "tesseract",
        which=lambda _: None,
        required=False,
        remediation="install language data",
    )

    assert result.status == "warning"
    assert result.remediation == "install language data"


def test_module_check_distinguishes_required_and_optional() -> None:
    missing = lambda _: None

    required = check_module(
        "Example", "example", "requirements.txt", find_spec=missing
    )
    optional = check_module(
        "Example",
        "example",
        "requirements-extra.txt",
        required=False,
        find_spec=missing,
    )

    assert required.status == "error"
    assert optional.status == "warning"


def test_sqlite_fts5_is_available() -> None:
    assert check_sqlite_fts5().status == "ok"


def test_hf_token_prefers_environment(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("HF_TOKEN=file-token\n", encoding="utf-8")

    token = load_hf_token(tmp_path, environ={"HF_TOKEN": "environment-token"})

    assert token == "environment-token"


def test_hf_token_supports_export_syntax(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "# comment\nexport HF_TOKEN='file-token'\n", encoding="utf-8"
    )

    assert load_hf_token(tmp_path, environ={}) == "file-token"


def test_report_exposes_errors_and_formats_remediation() -> None:
    report = PreflightReport(
        (
            CheckResult("python", "ok", "Python 3.13.5"),
            CheckResult(
                "ffmpeg",
                "error",
                "missing",
                "install ffmpeg",
            ),
        )
    )

    rendered = format_report(report, include_ok=False)

    assert not report.ok
    assert len(report.errors) == 1
    assert "[ERROR] ffmpeg: missing" in rendered
    assert "install ffmpeg" in rendered
    assert "python" not in rendered


def test_preflight_only_does_not_require_video(
    monkeypatch, capsys
) -> None:
    report = PreflightReport((CheckResult("python", "ok", "ready"),))
    monkeypatch.setattr(run_pipeline, "run_preflight", lambda _: report)
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py", "--preflight-only"])

    exit_code = run_pipeline.main()

    assert exit_code == 0
    assert "[OK] python: ready" in capsys.readouterr().out
