"""Loopback integration tests for the production HTTP provider."""

import pytest

from video_preprocess.inference import (
    EmbeddingService,
    HTTPInferenceProvider,
    InferenceGateway,
)

from tests.support.fake_inference_server import FakeInferenceServer


pytestmark = pytest.mark.integration


def test_embedding_service_runs_through_http_job_protocol() -> None:
    with FakeInferenceServer(auth_token="test-token") as server:
        provider = HTTPInferenceProvider(
            alias="embedding.remote",
            endpoint=server.base_url,
            auth_token="test-token",
            poll_interval_sec=0.001,
            max_poll_interval_sec=0.01,
        )
        gateway = InferenceGateway({"embedding.remote": provider})
        service = EmbeddingService(
            gateway,
            alias="embedding.remote",
            model_name="example/embedding",
            revision="main",
            timeout_sec=2,
        )

        batch = service.embed(["첫 번째", "두 번째"])

        assert batch.vectors == ((1.0, 0.0), (0.0, 1.0))
        assert batch.model.provider == "http.embedding"
        assert batch.model.revision == "fake-commit-1"
        assert server.service.inference_count == 1


def test_provider_recovers_existing_job_for_same_idempotency_key() -> None:
    with FakeInferenceServer() as server:
        provider = HTTPInferenceProvider(
            alias="embedding.remote",
            endpoint=server.base_url,
            poll_interval_sec=0.001,
            max_poll_interval_sec=0.01,
        )
        service = EmbeddingService(
            InferenceGateway({"embedding.remote": provider}),
            alias="embedding.remote",
            model_name="example/embedding",
            revision="main",
            timeout_sec=2,
        )

        first = service.embed(["같은 입력"])
        second = service.embed(["같은 입력"])

        assert first.vectors == second.vectors
        assert server.service.inference_count == 1
        assert len(server.service.jobs) == 1
