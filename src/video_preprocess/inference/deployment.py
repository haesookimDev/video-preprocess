"""Deployment settings and composition helpers for inference aliases."""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from urllib.parse import urlparse

from video_preprocess.storage import ArtifactStore

from .audio_event import AudioEventService
from .embedding import EmbeddingService
from .errors import ProviderConfigurationError
from .gateway import InferenceGateway
from .http import HTTPInferenceProvider, HTTPRetryPolicy
from .ocr import OCRService


@dataclass(frozen=True, slots=True)
class HTTPProviderSettings:
    """Serializable-safe configuration for one remote model alias."""

    endpoint: str
    auth_token: str | None = field(default=None, repr=False, compare=False)
    allowed_artifact_namespaces: Collection[str] = ()
    request_timeout_sec: float = 300.0
    operation_timeout_sec: float = 10.0
    poll_interval_sec: float = 0.1
    max_poll_interval_sec: float = 2.0
    capability_ttl_sec: float = 30.0
    retry_policy: HTTPRetryPolicy = field(default_factory=HTTPRetryPolicy)

    def __post_init__(self) -> None:
        endpoint = self._normalize_endpoint(self.endpoint)
        object.__setattr__(self, "endpoint", endpoint)
        if self.auth_token is not None:
            token = self._required_string(self.auth_token, "auth_token")
            object.__setattr__(self, "auth_token", token)
        namespaces = self.allowed_artifact_namespaces
        if isinstance(namespaces, (str, bytes)) or not isinstance(
            namespaces,
            Collection,
        ):
            raise TypeError("allowed_artifact_namespaces must be a collection")
        object.__setattr__(
            self,
            "allowed_artifact_namespaces",
            tuple(
                self._required_string(namespace, "artifact namespace")
                for namespace in namespaces
            ),
        )
        for field_name in (
            "request_timeout_sec",
            "operation_timeout_sec",
            "poll_interval_sec",
            "max_poll_interval_sec",
            "capability_ttl_sec",
        ):
            object.__setattr__(
                self,
                field_name,
                self._positive_number(getattr(self, field_name), field_name),
            )
        if self.max_poll_interval_sec < self.poll_interval_sec:
            raise ValueError(
                "max_poll_interval_sec must be at least poll_interval_sec"
            )
        if not isinstance(self.retry_policy, HTTPRetryPolicy):
            raise TypeError("retry_policy must be an HTTPRetryPolicy")

    def public_dict(self) -> dict[str, object]:
        """Return deployment metadata that is safe for CLI output and logs."""

        return {
            "provider": "http",
            "endpoint": self.endpoint,
            "allowed_artifact_namespaces": list(
                self.allowed_artifact_namespaces
            ),
            "request_timeout_sec": self.request_timeout_sec,
        }

    @staticmethod
    def _required_string(value: object, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} must be a non-empty string")
        return value.strip()

    @staticmethod
    def _positive_number(value: object, field_name: str) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value <= 0
        ):
            raise ValueError(f"{field_name} must be positive")
        return float(value)

    @classmethod
    def _normalize_endpoint(cls, endpoint: object) -> str:
        value = cls._required_string(endpoint, "endpoint").rstrip("/")
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("endpoint must be an absolute HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("endpoint must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("endpoint must not contain query or fragment")
        return value


@dataclass(frozen=True, slots=True)
class InferenceDeploymentSettings:
    """Remote alias map; each task composer defines its local fallback."""

    http_providers: Mapping[str, HTTPProviderSettings] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        if not isinstance(self.http_providers, Mapping):
            raise TypeError("http_providers must be a mapping")
        normalized = {}
        for alias, settings in self.http_providers.items():
            normalized_alias = HTTPProviderSettings._required_string(
                alias,
                "provider alias",
            )
            if not isinstance(settings, HTTPProviderSettings):
                raise TypeError(
                    "http_providers values must be HTTPProviderSettings"
                )
            normalized[normalized_alias] = settings
        object.__setattr__(self, "http_providers", normalized)

    def http_provider(self, alias: str) -> HTTPProviderSettings | None:
        return self.http_providers.get(alias)

    def public_dict(self) -> dict[str, dict[str, object]]:
        return {
            alias: settings.public_dict()
            for alias, settings in sorted(self.http_providers.items())
        }


def create_configured_embedding_service(
    model_name: str,
    *,
    deployments: InferenceDeploymentSettings | None = None,
    alias: str = "embedding.default",
    revision: str | None = None,
) -> EmbeddingService:
    """Compose the embedding alias locally or through its HTTP endpoint."""

    selected = deployments or InferenceDeploymentSettings()
    if not isinstance(selected, InferenceDeploymentSettings):
        raise TypeError("deployments must be InferenceDeploymentSettings")
    remote = selected.http_provider(alias)
    if remote is None:
        from .local import get_local_embedding_service

        return get_local_embedding_service(
            model_name,
            alias=alias,
            revision=revision,
        )
    provider = HTTPInferenceProvider(
        alias=alias,
        endpoint=remote.endpoint,
        auth_token=remote.auth_token,
        allowed_artifact_namespaces=remote.allowed_artifact_namespaces,
        operation_timeout_sec=remote.operation_timeout_sec,
        poll_interval_sec=remote.poll_interval_sec,
        max_poll_interval_sec=remote.max_poll_interval_sec,
        capability_ttl_sec=remote.capability_ttl_sec,
        retry_policy=remote.retry_policy,
    )
    gateway = InferenceGateway({alias: provider})
    return EmbeddingService(
        gateway,
        alias=alias,
        model_name=model_name,
        revision=revision or "default",
        timeout_sec=remote.request_timeout_sec,
    )


def create_configured_ocr_service(
    model_name: str,
    artifact_store: ArtifactStore,
    *,
    deployments: InferenceDeploymentSettings | None = None,
    alias: str = "ocr.default",
    revision: str | None = None,
    command: str = "tesseract",
    max_batch_size: int = 4,
) -> OCRService:
    """Compose the OCR alias locally or through its HTTP endpoint."""

    selected = deployments or InferenceDeploymentSettings()
    if not isinstance(selected, InferenceDeploymentSettings):
        raise TypeError("deployments must be InferenceDeploymentSettings")
    remote = selected.http_provider(alias)
    if remote is None:
        from .local import create_local_ocr_service

        return create_local_ocr_service(
            artifact_store,
            alias=alias,
            model_name=model_name,
            revision=revision or "system",
            command=command,
            max_batch_size=max_batch_size,
        )
    provider = HTTPInferenceProvider(
        alias=alias,
        endpoint=remote.endpoint,
        auth_token=remote.auth_token,
        allowed_artifact_namespaces=remote.allowed_artifact_namespaces,
        operation_timeout_sec=remote.operation_timeout_sec,
        poll_interval_sec=remote.poll_interval_sec,
        max_poll_interval_sec=remote.max_poll_interval_sec,
        capability_ttl_sec=remote.capability_ttl_sec,
        retry_policy=remote.retry_policy,
    )
    gateway = InferenceGateway({alias: provider})
    return OCRService(
        gateway,
        alias=alias,
        model_name=model_name,
        revision=revision or "default",
        timeout_sec=remote.request_timeout_sec,
        batch_size=max_batch_size,
    )


def create_configured_audio_event_service(
    model_name: str,
    *,
    deployments: InferenceDeploymentSettings | None = None,
    alias: str = "audio_event.default",
    revision: str | None = None,
    max_batch_size: int | None = None,
) -> AudioEventService:
    """Compose the current HTTP audio-event alias.

    The service boundary is also valid for an in-process Provider. The default
    runtime intentionally requires an explicit endpoint until a local model and
    its taxonomy mapping are implemented.
    """

    selected = deployments or InferenceDeploymentSettings()
    if not isinstance(selected, InferenceDeploymentSettings):
        raise TypeError("deployments must be InferenceDeploymentSettings")
    remote = selected.http_provider(alias)
    if remote is None:
        raise ProviderConfigurationError(
            "audio_event.default requires an HTTP endpoint until a local "
            "audio event provider is configured"
        )
    provider = HTTPInferenceProvider(
        alias=alias,
        endpoint=remote.endpoint,
        auth_token=remote.auth_token,
        allowed_artifact_namespaces=remote.allowed_artifact_namespaces,
        operation_timeout_sec=remote.operation_timeout_sec,
        poll_interval_sec=remote.poll_interval_sec,
        max_poll_interval_sec=remote.max_poll_interval_sec,
        capability_ttl_sec=remote.capability_ttl_sec,
        retry_policy=remote.retry_policy,
    )
    return AudioEventService(
        InferenceGateway({alias: provider}),
        alias=alias,
        model_name=model_name,
        revision=revision or "default",
        timeout_sec=remote.request_timeout_sec,
        batch_size=max_batch_size,
    )
