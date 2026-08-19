"""Tests for alias-based local/HTTP inference composition settings."""

from pathlib import Path

import pytest

from pipeline.deployment import (
    embedding_deployments_from_environment,
    inference_deployments_from_environment,
)
from video_preprocess.inference import (
    HTTPInferenceProvider,
    HTTPProviderSettings,
    InferenceDeploymentSettings,
    ProviderConfigurationError,
    create_configured_audio_event_service,
    create_configured_embedding_service,
    create_configured_ocr_service,
)
from video_preprocess.inference.local import (
    LocalEmbeddingProvider,
    LocalOCRProvider,
)
from video_preprocess.services import PipelineRunRequest
from video_preprocess.storage import LocalArtifactStore


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


def test_ocr_alias_selects_local_or_http_provider(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path, namespace="test")
    local = create_configured_ocr_service("tesseract", store)
    deployments = InferenceDeploymentSettings(
        http_providers={
            "ocr.default": HTTPProviderSettings(
                endpoint="https://ocr.example.test",
                request_timeout_sec=15,
            )
        }
    )
    remote = create_configured_ocr_service(
        "example/ocr",
        store,
        deployments=deployments,
    )

    assert isinstance(bound_provider(local, "ocr.default"), LocalOCRProvider)
    assert isinstance(
        bound_provider(remote, "ocr.default"),
        HTTPInferenceProvider,
    )
    assert remote.timeout_sec == 15


def test_audio_event_alias_requires_endpoint_and_selects_http() -> None:
    with pytest.raises(ProviderConfigurationError, match="HTTP endpoint"):
        create_configured_audio_event_service("example/audio-event")
    deployments = InferenceDeploymentSettings(
        http_providers={
            "audio_event.default": HTTPProviderSettings(
                endpoint="https://audio.example.test",
                request_timeout_sec=20,
            )
        }
    )

    service = create_configured_audio_event_service(
        "example/audio-event",
        deployments=deployments,
        max_batch_size=6,
    )

    assert isinstance(
        bound_provider(service, "audio_event.default"),
        HTTPInferenceProvider,
    )
    assert service.timeout_sec == 20
    assert service.batch_size == 6


def test_environment_adapter_composes_inference_aliases() -> None:
    deployments = inference_deployments_from_environment(
        endpoints={
            "audio_event.default": "https://audio.example.test",
            "embedding.default": "https://models.example.test",
            "ocr.default": "https://ocr.example.test",
        },
        token_envs={
            "audio_event.default": None,
            "embedding.default": None,
            "ocr.default": "OCR_TOKEN",
        },
        artifact_namespaces={
            "audio_event.default": ("run-artifacts",),
            "embedding.default": (),
            "ocr.default": ("run-artifacts",),
        },
        environ={"OCR_TOKEN": "secret-value"},
    )

    assert set(deployments.http_providers) == {
        "audio_event.default",
        "embedding.default",
        "ocr.default",
    }
    ocr = deployments.http_provider("ocr.default")
    assert ocr is not None
    assert ocr.auth_token == "secret-value"
    assert ocr.allowed_artifact_namespaces == ("run-artifacts",)
    assert "secret-value" not in str(deployments.public_dict())


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
