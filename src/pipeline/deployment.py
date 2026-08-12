"""CLI-facing helpers for selecting inference deployments safely."""

from __future__ import annotations

from collections.abc import Collection, Mapping

from video_preprocess.inference import (
    HTTPProviderSettings,
    InferenceDeploymentSettings,
)


def embedding_deployments_from_environment(
    *,
    endpoint: str | None,
    token_env: str | None,
    artifact_namespaces: Collection[str],
    environ: Mapping[str, str],
) -> InferenceDeploymentSettings:
    """Build the embedding alias config without storing an env-var name."""

    if endpoint is None:
        if token_env is not None:
            raise ValueError(
                "embedding_token_env requires embedding_endpoint"
            )
        if artifact_namespaces:
            raise ValueError(
                "embedding_artifact_namespace requires embedding_endpoint"
            )
        return InferenceDeploymentSettings()
    token = None
    if token_env is not None:
        if not isinstance(token_env, str) or not token_env.strip():
            raise ValueError("embedding_token_env must be non-empty")
        variable = token_env.strip()
        token = environ.get(variable, "").strip()
        if not token:
            raise ValueError(
                "embedding token environment variable is empty: "
                f"{variable}"
            )
    return InferenceDeploymentSettings(
        http_providers={
            "embedding.default": HTTPProviderSettings(
                endpoint=endpoint,
                auth_token=token,
                allowed_artifact_namespaces=artifact_namespaces,
            )
        }
    )
