"""Tests for the production Pipeline API server CLI."""

import sys

import serve_pipeline


def test_cli_composes_server_without_printing_token(monkeypatch, capsys) -> None:
    seen = {}

    class Server:
        base_url = "http://127.0.0.1:8090"

        def __init__(self, **options):
            seen["server"] = options

        def serve_forever(self):
            return None

    class RunService:
        def __init__(self, *args, **options):
            seen["run_service"] = options

        def get(self, run_id):
            raise AssertionError("CLI composition must not query a run")

    monkeypatch.setattr(serve_pipeline, "PipelineHTTPServer", Server)
    monkeypatch.setattr(serve_pipeline, "PipelineRunService", RunService)
    monkeypatch.setattr(
        serve_pipeline,
        "LocalPipelineRunRepository",
        lambda path: ("repository", path),
    )
    monkeypatch.setattr(
        serve_pipeline,
        "LocalMediaCatalog",
        lambda path: ("catalog", path),
    )
    monkeypatch.setenv("PIPELINE_TOKEN", "private-token")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "serve_pipeline.py",
            "--auth-token-env",
            "PIPELINE_TOKEN",
            "--max-active-runs",
            "3",
        ],
    )

    assert serve_pipeline.main() == 0
    output = capsys.readouterr().out
    assert seen["server"]["auth_token"] == "private-token"
    assert seen["run_service"]["max_active_runs"] == 3
    assert "private-token" not in output
    assert "PIPELINE_TOKEN" not in output


def test_cli_rejects_missing_token_environment(monkeypatch, capsys) -> None:
    monkeypatch.delenv("MISSING_TOKEN", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "serve_pipeline.py",
            "--auth-token-env",
            "MISSING_TOKEN",
        ],
    )

    assert serve_pipeline.main() == 2
    assert "environment variable is empty" in capsys.readouterr().err
