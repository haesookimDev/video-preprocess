"""Explicit real-model E2E for the production inference server."""

import asyncio
import math

import pytest

from video_preprocess.inference import (
    EmbeddingService,
    HTTPInferenceProvider,
    InferenceGateway,
    InferenceHTTPServer,
)
from video_preprocess.inference.local import LocalEmbeddingProvider


pytestmark = [pytest.mark.integration, pytest.mark.model]


def test_real_sentence_transformer_runs_through_server_http_boundary() -> None:
    model_name = "paraphrase-multilingual-MiniLM-L12-v2"
    provider = LocalEmbeddingProvider(
        alias="embedding.default",
        model_name=model_name,
    )
    asyncio.run(provider.warmup())
    with InferenceHTTPServer(
        alias="embedding.default",
        provider=provider,
        host="127.0.0.1",
        port=0,
    ) as server:
        client = HTTPInferenceProvider(
            alias="embedding.default",
            endpoint=server.base_url,
            poll_interval_sec=0.01,
            max_poll_interval_sec=0.1,
        )
        service = EmbeddingService(
            InferenceGateway({"embedding.default": client}),
            alias="embedding.default",
            model_name=model_name,
            revision="default",
            timeout_sec=30,
        )

        batch = service.embed(["영상 전처리", "음성 구간 검출"])

    assert batch.dimension == 384
    assert len(batch.vectors) == 2
    assert math.sqrt(sum(value * value for value in batch.vectors[0])) == (
        pytest.approx(1.0)
    )
    assert batch.model.provider == "local.embedding"
