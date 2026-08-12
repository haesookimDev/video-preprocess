"""Tests for the production inference server CLI adapter."""

import sys

import serve_inference


def test_cli_composes_server_without_printing_token(monkeypatch, capsys) -> None:
    seen = {}

    class Server:
        base_url = "http://127.0.0.1:8080"

        def __init__(self, **options):
            seen.update(options)

        def serve_forever(self):
            return None

    monkeypatch.setattr(serve_inference, "InferenceHTTPServer", Server)
    monkeypatch.setenv("MODEL_SERVER_TOKEN", "private-token")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "serve_inference.py",
            "--model",
            "example/model",
            "--auth-token-env",
            "MODEL_SERVER_TOKEN",
        ],
    )

    assert serve_inference.main() == 0
    output = capsys.readouterr().out
    assert seen["alias"] == "embedding.default"
    assert seen["auth_token"] == "private-token"
    assert "example/model" in output
    assert "private-token" not in output
    assert "MODEL_SERVER_TOKEN" not in output


def test_cli_rejects_missing_token_environment(monkeypatch, capsys) -> None:
    monkeypatch.delenv("MISSING_TOKEN", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "serve_inference.py",
            "--auth-token-env",
            "MISSING_TOKEN",
        ],
    )

    assert serve_inference.main() == 2
    assert "environment variable is empty" in capsys.readouterr().err
