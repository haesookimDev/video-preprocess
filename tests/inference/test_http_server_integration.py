"""Production server/client integration using the LocalEmbeddingProvider."""

import asyncio

import pytest

from video_preprocess.inference import (
    EmbeddingService,
    HTTPInferenceProvider,
    InferenceGateway,
    InferenceHTTPServer,
)
from video_preprocess.inference.local import LocalEmbeddingProvider


pytestmark = pytest.mark.integration


class TinyEmbeddingModel:
    def encode(self, sentences, *, normalize_embeddings):
        assert normalize_embeddings
        return [[float(index + 1), 1.0] for index, _ in enumerate(sentences)]


def test_production_server_runs_local_provider_through_http() -> None:
    provider = LocalEmbeddingProvider(
        alias="embedding.default",
        model_name="example/embedding",
        revision="test-revision",
        loader=lambda *_: TinyEmbeddingModel(),
    )
    asyncio.run(provider.warmup())
    with InferenceHTTPServer(
        alias="embedding.default",
        provider=provider,
        host="127.0.0.1",
        port=0,
        auth_token="server-token",
    ) as server:
        client = HTTPInferenceProvider(
            alias="embedding.default",
            endpoint=server.base_url,
            auth_token="server-token",
            poll_interval_sec=0.001,
            max_poll_interval_sec=0.01,
        )
        service = EmbeddingService(
            InferenceGateway({"embedding.default": client}),
            alias="embedding.default",
            model_name="example/embedding",
            revision="test-revision",
            timeout_sec=2,
        )

        batch = service.embed(["첫 번째", "두 번째"])
        effective = asyncio.run(client.effective_model())

    assert batch.dimension == 2
    assert batch.model.provider == "local.embedding"
    assert batch.model.revision == "test-revision"
    assert effective is not None
    assert effective.revision == "test-revision"
    assert provider.is_loaded
