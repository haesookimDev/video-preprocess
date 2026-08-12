"""Tests for alias-based local/HTTP inference composition settings."""

from pathlib import Path

import pytest

from pipeline.deployment import embedding_deployments_from_environment
from video_preprocess.inference import (
    HTTPInferenceProvider,
    HTTPProviderSettings,
    InferenceDeploymentSettings,
    create_configured_embedding_service,
)
from video_preprocess.inference.local import LocalEmbeddingProvider
from video_preprocess.services import PipelineRunRequest


def bound_provider(service, alias="embedding.default"):
    return service.gateway._bindings[alias]


def test_embedding_alias_uses_local_provider_when_unconfigured() -> None:
    service = create_configured_embedding_service("example/embedding")

    assert isinstance(bound_provider(service), LocalEmbeddingProvider)


def test_embedding_alias_uses_http_provider_when_endpoint_is_configured() -> None:
    deployments = InferenceDeploymentSettings(
        http_providers={
            "embedding.default": HTTPProviderSettings(
                endpoint="https://models.example.test/inference/",
                auth_token="private-token",
                request_timeout_sec=12,
            )
        }
    )

    service = create_configured_embedding_service(
        "example/embedding",
        deployments=deployments,
    )

    provider = bound_provider(service)
    assert isinstance(provider, HTTPInferenceProvider)
    assert provider.endpoint == "https://models.example.test/inference"
    assert service.timeout_sec == 12
    assert "private-token" not in repr(deployments)
    assert "private-token" not in str(deployments.public_dict())


def test_environment_adapter_resolves_token_without_exposing_variable() -> None:
    deployments = embedding_deployments_from_environment(
        endpoint="https://models.example.test",
        token_env="MODEL_TOKEN",
        artifact_namespaces=("shared-artifacts",),
        environ={"MODEL_TOKEN": "secret-value"},
    )

    public = deployments.public_dict()["embedding.default"]
    provider = deployments.http_provider("embedding.default")
    assert provider is not None
    assert provider.auth_token == "secret-value"
    assert public["provider"] == "http"
    assert public["allowed_artifact_namespaces"] == ["shared-artifacts"]
    assert "MODEL_TOKEN" not in str(public)
    assert "secret-value" not in str(public)


@pytest.mark.parametrize(
    ("endpoint", "token_env", "namespaces", "message"),
    [
        (None, "MODEL_TOKEN", (), "requires embedding_endpoint"),
        (None, None, ("shared",), "requires embedding_endpoint"),
        (
            "https://models.example.test",
            "MISSING_TOKEN",
            (),
            "environment variable is empty",
        ),
    ],
)
def test_environment_adapter_rejects_incomplete_remote_config(
    endpoint,
    token_env,
    namespaces,
    message,
) -> None:
    with pytest.raises(ValueError, match=message):
        embedding_deployments_from_environment(
            endpoint=endpoint,
            token_env=token_env,
            artifact_namespaces=namespaces,
            environ={},
        )


def test_pipeline_request_rejects_untyped_deployment_settings(
    tmp_path: Path,
) -> None:
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")

    with pytest.raises(TypeError, match="deployments"):
        PipelineRunRequest(
            video_path=video,
            output_root=tmp_path / "output",
            deployments={"embedding.default": "http"},
        )
