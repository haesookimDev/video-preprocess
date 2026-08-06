"""모델을 로드하지 않고 실행 환경의 필수 조건을 검사한다."""

from __future__ import annotations

import importlib.util
import os
import shutil
import sqlite3
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


CheckStatus = Literal["ok", "warning", "error"]
MIN_PYTHON = (3, 10)

REQUIRED_MODULES = (
    ("NumPy", "numpy", "requirements.txt"),
    ("PySceneDetect", "scenedetect", "requirements.txt"),
    ("faster-whisper", "faster_whisper", "requirements.txt"),
    ("Pillow", "PIL", "requirements.txt"),
    ("Transformers", "transformers", "requirements.txt"),
    (
        "Sentence Transformers",
        "sentence_transformers",
        "requirements.txt",
    ),
)

DIARIZATION_MODULES = (
    ("Hugging Face Hub", "huggingface_hub"),
    ("pyannote.audio", "pyannote.audio"),
)


@dataclass(frozen=True)
class CheckResult:
    """하나의 환경 검사 결과."""

    name: str
    status: CheckStatus
    detail: str
    remediation: str | None = None


@dataclass(frozen=True)
class PreflightReport:
    """환경 검사 전체 결과."""

    checks: tuple[CheckResult, ...]

    @property
    def ok(self) -> bool:
        return not any(check.status == "error" for check in self.checks)

    @property
    def warnings(self) -> tuple[CheckResult, ...]:
        return tuple(c for c in self.checks if c.status == "warning")

    @property
    def errors(self) -> tuple[CheckResult, ...]:
        return tuple(c for c in self.checks if c.status == "error")


def check_python(version: tuple[int, ...] | None = None) -> CheckResult:
    """지원하는 Python 버전인지 확인한다."""
    current = version or tuple(sys.version_info[:3])
    rendered = ".".join(str(part) for part in current)
    if current[:2] >= MIN_PYTHON:
        return CheckResult("python", "ok", f"Python {rendered}")
    minimum = ".".join(str(part) for part in MIN_PYTHON)
    return CheckResult(
        "python",
        "error",
        f"Python {rendered}은 지원하지 않음",
        f"Python {minimum} 이상을 사용하세요.",
    )


def check_command(
    command: str,
    which: Callable[[str], str | None] = shutil.which,
) -> CheckResult:
    """필수 네이티브 명령이 PATH에 있는지 확인한다."""
    path = which(command)
    if path:
        return CheckResult(command, "ok", path)
    return CheckResult(
        command,
        "error",
        f"{command} 명령을 PATH에서 찾을 수 없음",
        "FFmpeg를 설치하고 PATH 설정을 확인하세요.",
    )


def check_sqlite_fts5(
    connect: Callable[[str], sqlite3.Connection] = sqlite3.connect,
) -> CheckResult:
    """현재 Python SQLite 빌드에서 FTS5를 사용할 수 있는지 확인한다."""
    db = connect(":memory:")
    try:
        db.execute("CREATE VIRTUAL TABLE preflight_fts USING fts5(content)")
    except sqlite3.Error as exc:
        return CheckResult(
            "sqlite_fts5",
            "error",
            f"SQLite FTS5를 사용할 수 없음: {exc}",
            "FTS5가 포함된 SQLite/Python 빌드를 사용하세요.",
        )
    finally:
        db.close()
    return CheckResult("sqlite_fts5", "ok", f"SQLite {sqlite3.sqlite_version}")


def check_module(
    display_name: str,
    module: str,
    install_file: str,
    *,
    required: bool = True,
    find_spec: Callable[[str], object | None] = importlib.util.find_spec,
) -> CheckResult:
    """패키지를 import하지 않고 모듈 spec 존재 여부만 확인한다."""
    try:
        available = find_spec(module) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        available = False
    if available:
        return CheckResult(f"module:{module}", "ok", f"{display_name} 사용 가능")

    status: CheckStatus = "error" if required else "warning"
    qualifier = "필수" if required else "선택"
    return CheckResult(
        f"module:{module}",
        status,
        f"{display_name} {qualifier} 모듈이 설치되지 않음",
        f".venv/bin/pip install -r {install_file}",
    )


def load_hf_token(
    project_root: Path,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """환경변수 또는 프로젝트 .env에서 HF_TOKEN을 읽는다."""
    environment = os.environ if environ is None else environ
    token = environment.get("HF_TOKEN", "").strip()
    if token:
        return token

    env_file = project_root / ".env"
    if not env_file.exists():
        return None
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        key, separator, value = line.partition("=")
        if separator and key.strip() == "HF_TOKEN":
            parsed = value.strip().strip('"').strip("'")
            return parsed or None
    return None


def run_preflight(project_root: Path) -> PreflightReport:
    """현재 로컬 MVP 실행에 필요한 환경을 검사한다."""
    checks = [
        check_python(),
        check_command("ffmpeg"),
        check_command("ffprobe"),
        check_sqlite_fts5(),
    ]
    for display_name, module, install_file in REQUIRED_MODULES:
        checks.append(check_module(display_name, module, install_file))

    hf_token = load_hf_token(project_root)
    if hf_token:
        checks.append(
            CheckResult("credential:HF_TOKEN", "ok", "HF_TOKEN 설정됨")
        )
    else:
        checks.append(
            CheckResult(
                "credential:HF_TOKEN",
                "warning",
                "HF_TOKEN이 없어 화자 분리 단계가 스킵됨",
                "화자 분리가 필요하면 프로젝트 .env 또는 환경변수에 설정하세요.",
            )
        )

    for display_name, module in DIARIZATION_MODULES:
        checks.append(
            check_module(
                display_name,
                module,
                "requirements-diarization.txt",
                required=bool(hf_token),
            )
        )
    return PreflightReport(tuple(checks))


def format_report(report: PreflightReport, *, include_ok: bool = True) -> str:
    """CLI에 표시할 환경 검사 결과를 만든다."""
    labels = {"ok": "OK", "warning": "WARN", "error": "ERROR"}
    lines = []
    for check in report.checks:
        if check.status == "ok" and not include_ok:
            continue
        lines.append(f"[{labels[check.status]}] {check.name}: {check.detail}")
        if check.remediation:
            lines.append(f"       해결: {check.remediation}")
    if not lines:
        return "환경 사전 검사 통과"
    return "\n".join(lines)

