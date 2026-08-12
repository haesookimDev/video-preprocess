"""Tests for safe local and Gateway effective model resolution."""

import asyncio
from pathlib import Path

from video_preprocess.domain import EffectiveModel, StageTask
from video_preprocess.inference import (
    GatewayEffectiveModelResolver,
    InferenceGateway,
)
from video_preprocess.inference.local import fingerprints


class FingerprintProvider:
    def __init__(self, model):
        self.model = model

    async def capabilities(self):
        raise AssertionError("capabilities must not be called")

    async def infer(self, request):
        raise AssertionError("infer must not be called")

    async def cancel(self, request_id):
        raise AssertionError("cancel must not be called")

    async def health(self):
        raise AssertionError("health must not be called")

    async def effective_model(self):
        return self.model


def task(*, bindings=None):
    return StageTask(
        run_id="run-123",
        stage_run_id="stage-123",
        attempt=1,
        stage="06_stt",
        stage_version="1.0.0",
        inputs={},
        config={},
        model_bindings=(
            {"stt": "stt.default"} if bindings is None else bindings
        ),
        idempotency_key="idem-123",
        trace_id="trace-123",
    )


def test_gateway_resolver_converts_effective_models_by_slot() -> None:
    effective = EffectiveModel(
        provider="local.stt",
        name="base",
        revision="commit-123",
        runtime="faster-whisper/test",
    )
    gateway = InferenceGateway(
        {"stt.default": FingerprintProvider(effective)}
    )
    resolver = GatewayEffectiveModelResolver({"stt.default": gateway})

    models = asyncio.run(resolver.resolve(task()))

    assert models is not None
    assert [model.to_dict() for model in models] == [
        {
            "slot": "stt",
            "provider": "local.stt",
            "model": "base",
            "revision": "commit-123",
            "runtime": "faster-whisper/test",
        }
    ]


def test_gateway_resolver_returns_none_for_unresolved_or_unknown_alias() -> None:
    gateway = InferenceGateway(
        {"stt.default": FingerprintProvider(None)}
    )
    resolver = GatewayEffectiveModelResolver({"stt.default": gateway})

    assert asyncio.run(resolver.resolve(task())) is None
    assert asyncio.run(
        resolver.resolve(task(bindings={"stt": "stt.remote"}))
    ) is None


def test_mutable_hub_revision_is_not_resolved_while_online(
    monkeypatch,
) -> None:
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)

    assert fingerprints.resolve_hf_cache_revision(
        "example/model",
        "config.json",
        None,
    ) is None
    assert fingerprints.resolve_hf_cache_revision(
        "example/model",
        "config.json",
        "main",
    ) is None


def test_offline_hub_revision_uses_only_cached_snapshot(
    monkeypatch,
) -> None:
    import huggingface_hub

    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    calls = []

    def cached(**options):
        calls.append(options)
        return str(
            Path("cache")
            / "models--example--model"
            / "snapshots"
            / "abcdef123456"
            / "config.json"
        )

    monkeypatch.setattr(huggingface_hub, "try_to_load_from_cache", cached)

    assert fingerprints.resolve_hf_cache_revision(
        "example/model",
        "config.json",
        None,
    ) == "abcdef123456"
    assert calls == [
        {
            "repo_id": "example/model",
            "filename": "config.json",
            "revision": None,
        }
    ]
