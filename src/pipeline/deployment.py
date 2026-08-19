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

    return inference_deployments_from_environment(
        endpoints={"embedding.default": endpoint},
        token_envs={"embedding.default": token_env},
        artifact_namespaces={
            "embedding.default": artifact_namespaces,
        },
        environ=environ,
    )


def inference_deployments_from_environment(
    *,
    endpoints: Mapping[str, str | None],
    token_envs: Mapping[str, str | None],
    artifact_namespaces: Mapping[str, Collection[str]],
    environ: Mapping[str, str],
) -> InferenceDeploymentSettings:
    """Build multiple alias configs while resolving secret env variables."""

    aliases = set(endpoints) | set(token_envs) | set(artifact_namespaces)
    providers = {}
    for alias in sorted(aliases):
        endpoint = endpoints.get(alias)
        token_env = token_envs.get(alias)
        namespaces = artifact_namespaces.get(alias, ())
        option_prefix = alias.removesuffix(".default")
        if endpoint is None:
            if token_env is not None:
                raise ValueError(
                    f"{option_prefix}_token_env requires "
                    f"{option_prefix}_endpoint"
                )
            if namespaces:
                raise ValueError(
                    f"{option_prefix}_artifact_namespace requires "
                    f"{option_prefix}_endpoint"
                )
            continue
        token = None
        if token_env is not None:
            if not isinstance(token_env, str) or not token_env.strip():
                raise ValueError(
                    f"{option_prefix}_token_env must be non-empty"
                )
            variable = token_env.strip()
            token = environ.get(variable, "").strip()
            if not token:
                raise ValueError(
                    f"{option_prefix} token environment variable is empty: "
                    f"{variable}"
                )
        providers[alias] = HTTPProviderSettings(
            endpoint=endpoint,
            auth_token=token,
            allowed_artifact_namespaces=namespaces,
        )
    return InferenceDeploymentSettings(http_providers=providers)
