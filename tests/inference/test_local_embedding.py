"""Tests for lazy LocalEmbeddingProvider lifecycle and normalization."""

import asyncio
import math
from dataclasses import replace
from types import SimpleNamespace

import pytest

from video_preprocess.domain import (
    InferenceErrorCode,
    InferenceRequest,
    InferenceStatus,
    InferenceTask,
    RequestedModel,
)
from video_preprocess.inference import (
    EmbeddingService,
    InferenceCallError,
    InferenceGateway,
)
from video_preprocess.inference.local import LocalEmbeddingProvider


class FakeModel:
    def __init__(self) -> None:
        self.encode_count = 0

    def encode(self, sentences, *, normalize_embeddings):
        self.encode_count += 1
        assert normalize_embeddings
        return [[float(index), 1.0] for index, _ in enumerate(sentences)]


def make_provider(loader):
    return LocalEmbeddingProvider(
        alias="embedding.default",
        model_name="example/model",
        revision="main",
        loader=loader,
    )


def make_request(
    texts: list[str],
    *,
    request_id: str,
    idempotency_key: str,
) -> InferenceRequest:
    return InferenceRequest(
        request_id=request_id,
        idempotency_key=idempotency_key,
        run_id="run_123",
        stage_run_id="stage_456",
        task=InferenceTask.TEXT_EMBEDDING,
        model=RequestedModel(
            alias="embedding.default",
            name="example/model",
            revision="main",
        ),
        inputs={"texts": texts},
        parameters={"normalize_embeddings": True},
        timeout_sec=10,
        trace_id=f"trace_{request_id}",
    )


def test_provider_lazy_loads_once_and_reuses_idempotent_result() -> None:
    model = FakeModel()
    load_count = 0

    def loader(model_name, revision, device):
        nonlocal load_count
        load_count += 1
        assert (model_name, revision, device) == (
            "example/model",
            "main",
            None,
        )
        return model

    provider = make_provider(loader)
    gateway = InferenceGateway({"embedding.default": provider})
    service = EmbeddingService(
        gateway,
        alias="embedding.default",
        model_name="example/model",
        revision="main",
    )

    first = service.embed(["하나", "둘"])
    second = service.embed(["하나", "둘"])

    assert first.vectors[0] == (0.0, 1.0)
    assert first.vectors[1] == pytest.approx(
        (1 / math.sqrt(2), 1 / math.sqrt(2))
    )
    assert second.vectors == first.vectors
    assert first.model.revision == "main"
    assert load_count == 1
    assert model.encode_count == 1
    assert provider.is_loaded


def test_provider_reuses_model_for_different_requests() -> None:
    model = FakeModel()
    provider = make_provider(lambda *_: model)
    service = EmbeddingService(
        InferenceGateway({"embedding.default": provider}),
        alias="embedding.default",
        model_name="example/model",
        revision="main",
    )

    service.embed(["첫 요청"])
    service.embed(["다른 요청"])

    assert model.encode_count == 2
    assert provider.is_loaded


def test_default_binding_records_resolved_hugging_face_revision() -> None:
    class SnapshotModel(FakeModel):
        def _first_module(self):
            config = SimpleNamespace(_commit_hash="commit_abc123")
            return SimpleNamespace(
                auto_model=SimpleNamespace(config=config)
            )

    model = SnapshotModel()
    loaded_revision = "not-called"

    def loader(model_name, revision, device):
        nonlocal loaded_revision
        loaded_revision = revision
        return model

    provider = LocalEmbeddingProvider(
        alias="embedding.default",
        model_name="example/model",
        loader=loader,
    )
    service = EmbeddingService(
        InferenceGateway({"embedding.default": provider}),
        alias="embedding.default",
        model_name="example/model",
        revision=provider.requested_revision,
    )

    first = service.embed(["입력"])
    second = service.embed(["입력"])

    assert loaded_revision is None
    assert first.model.revision == "commit_abc123"
    assert second.model.revision == "commit_abc123"
    assert model.encode_count == 1


def test_idempotency_key_conflict_is_rejected() -> None:
    provider = make_provider(lambda *_: FakeModel())
    first = make_request(
        ["첫 입력"],
        request_id="infer_1",
        idempotency_key="same_key",
    )
    conflicting = replace(
        make_request(
            ["다른 입력"],
            request_id="infer_2",
            idempotency_key="same_key",
        ),
        trace_id="trace_different",
    )

    assert asyncio.run(provider.infer(first)).status is InferenceStatus.SUCCEEDED
    response = asyncio.run(provider.infer(conflicting))

    assert response.error is not None
    assert response.error.code is InferenceErrorCode.INVALID_REQUEST
    assert response.error.details["reason"] == "IDEMPOTENCY_KEY_CONFLICT"


def test_model_load_failure_is_normalized() -> None:
    def fail_loader(*_):
        raise RuntimeError("cannot load")

    provider = make_provider(fail_loader)
    service = EmbeddingService(
        InferenceGateway({"embedding.default": provider}),
        alias="embedding.default",
        model_name="example/model",
        revision="main",
    )

    with pytest.raises(InferenceCallError) as exc_info:
        service.embed(["입력"])

    assert exc_info.value.failure.code is InferenceErrorCode.MODEL_UNAVAILABLE


def test_provider_capabilities_do_not_load_model() -> None:
    load_count = 0

    def loader(*_):
        nonlocal load_count
        load_count += 1
        return FakeModel()

    provider = make_provider(loader)

    capabilities = asyncio.run(provider.capabilities())

    assert capabilities.model_aliases == ("embedding.default",)
    assert capabilities.tasks == (InferenceTask.TEXT_EMBEDDING,)
    assert load_count == 0
    assert not provider.is_loaded
