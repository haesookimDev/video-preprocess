"""Versioned contracts shared by inference gateways and providers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum

from ._validation import (
    JSONValue,
    SCHEMA_VERSION,
    normalize_json,
    normalize_json_object,
    normalize_string_tuple,
    optional_string,
    require_integer,
    require_mapping,
    require_number,
    require_schema_version,
    require_string,
)
from .artifacts import ArtifactRef
from .errors import ContractValidationError


class InferenceTask(str, Enum):
    """Task names understood by the inference boundary."""

    VOICE_ACTIVITY_DETECTION = "voice_activity_detection"
    SPEECH_TO_TEXT = "speech_to_text"
    SPEAKER_DIARIZATION = "speaker_diarization"
    IMAGE_CAPTIONING = "image_captioning"
    TEXT_EMBEDDING = "text_embedding"


class InferenceStatus(str, Enum):
    """Terminal status of one inference request."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class InferenceErrorCode(str, Enum):
    """Stable error categories shared by local and remote providers."""

    INVALID_REQUEST = "INVALID_REQUEST"
    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
    ARTIFACT_NOT_FOUND = "ARTIFACT_NOT_FOUND"
    ARTIFACT_INTEGRITY_ERROR = "ARTIFACT_INTEGRITY_ERROR"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    MODEL_ACCESS_DENIED = "MODEL_ACCESS_DENIED"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    PROVIDER_RATE_LIMITED = "PROVIDER_RATE_LIMITED"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    INFERENCE_FAILED = "INFERENCE_FAILED"
    CANCELLED = "CANCELLED"


class HealthState(str, Enum):
    """Current provider service state."""

    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


InferenceValue = (
    JSONValue
    | ArtifactRef
    | list["InferenceValue"]
    | dict[str, "InferenceValue"]
)


_ARTIFACT_FIELDS = {
    "schema_version",
    "artifact_id",
    "kind",
    "uri",
    "media_type",
    "size_bytes",
    "checksum",
}


def _enum_value(value: object, enum_type: type[Enum], field_name: str) -> Enum:
    if isinstance(value, enum_type):
        return value
    if isinstance(value, str):
        try:
            return enum_type(value)
        except ValueError as exc:
            raise ContractValidationError(
                field_name, f"unknown value {value!r}"
            ) from exc
    raise ContractValidationError(field_name, "must be a string enum value")


def _normalize_inference_values(
    value: object,
    field_name: str,
) -> dict[str, InferenceValue]:
    mapping = require_mapping(value, field_name)
    normalized: dict[str, InferenceValue] = {}
    for key, item in mapping.items():
        require_string(key, f"{field_name}.key")
        normalized[key] = _normalize_inference_value(
            item,
            f"{field_name}.{key}",
        )
    return normalized


def _normalize_inference_value(
    value: object,
    field_name: str,
) -> InferenceValue:
    if isinstance(value, ArtifactRef):
        return value
    if isinstance(value, Mapping):
        normalized: dict[str, InferenceValue] = {}
        for key, item in value.items():
            require_string(key, f"{field_name}.key")
            normalized[key] = _normalize_inference_value(
                item,
                f"{field_name}.{key}",
            )
        return normalized
    if isinstance(value, (list, tuple)):
        return [
            _normalize_inference_value(item, f"{field_name}[{index}]")
            for index, item in enumerate(value)
        ]
    return normalize_json(value, field_name)


def _inference_values_from_dict(
    value: object,
    field_name: str,
) -> dict[str, InferenceValue]:
    mapping = require_mapping(value, field_name)
    normalized: dict[str, InferenceValue] = {}
    for key, item in mapping.items():
        normalized[key] = _inference_value_from_dict(
            item,
            f"{field_name}.{key}",
        )
    return normalized


def _inference_value_from_dict(
    value: object,
    field_name: str,
) -> InferenceValue:
    if isinstance(value, Mapping):
        if _ARTIFACT_FIELDS.issubset(value):
            return ArtifactRef.from_dict(value)
        normalized: dict[str, InferenceValue] = {}
        for key, item in value.items():
            require_string(key, f"{field_name}.key")
            normalized[key] = _inference_value_from_dict(
                item,
                f"{field_name}.{key}",
            )
        return normalized
    if isinstance(value, (list, tuple)):
        return [
            _inference_value_from_dict(item, f"{field_name}[{index}]")
            for index, item in enumerate(value)
        ]
    return normalize_json(value, field_name)


def _inference_values_to_dict(
    values: Mapping[str, InferenceValue],
) -> dict[str, object]:
    return {
        key: _inference_value_to_dict(value)
        for key, value in values.items()
    }


def _inference_value_to_dict(value: InferenceValue) -> object:
    if isinstance(value, ArtifactRef):
        return value.to_dict()
    if isinstance(value, list):
        return [_inference_value_to_dict(item) for item in value]
    if isinstance(value, Mapping):
        return {
            key: _inference_value_to_dict(item)
            for key, item in value.items()
        }
    return value


@dataclass(frozen=True, slots=True)
class RequestedModel:
    """Logical alias and requested concrete model revision."""

    alias: str
    name: str
    revision: str

    def __post_init__(self) -> None:
        for field_name in ("alias", "name", "revision"):
            object.__setattr__(
                self,
                field_name,
                require_string(getattr(self, field_name), field_name),
            )

    def to_dict(self) -> dict[str, str]:
        return {
            "alias": self.alias,
            "name": self.name,
            "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "RequestedModel":
        mapping = require_mapping(data, "model")
        return cls(
            alias=require_string(mapping.get("alias"), "model.alias"),
            name=require_string(mapping.get("name"), "model.name"),
            revision=require_string(
                mapping.get("revision"), "model.revision"
            ),
        )


@dataclass(frozen=True, slots=True)
class EffectiveModel:
    """Concrete provider, model, and runtime used for a result."""

    provider: str
    name: str
    revision: str
    runtime: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("provider", "name", "revision"):
            object.__setattr__(
                self,
                field_name,
                require_string(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self, "runtime", optional_string(self.runtime, "runtime")
        )

    def to_dict(self) -> dict[str, str]:
        data = {
            "provider": self.provider,
            "name": self.name,
            "revision": self.revision,
        }
        if self.runtime is not None:
            data["runtime"] = self.runtime
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "EffectiveModel":
        mapping = require_mapping(data, "model")
        return cls(
            provider=require_string(
                mapping.get("provider"), "model.provider"
            ),
            name=require_string(mapping.get("name"), "model.name"),
            revision=require_string(
                mapping.get("revision"), "model.revision"
            ),
            runtime=optional_string(mapping.get("runtime"), "model.runtime"),
        )


@dataclass(frozen=True, slots=True)
class InferenceFailure:
    """Serializable inference failure returned across process boundaries."""

    code: InferenceErrorCode
    message: str
    retryable: bool
    details: Mapping[str, JSONValue] = field(default_factory=dict)
    request_id: str | None = None
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "schema_version",
            require_schema_version(self.schema_version),
        )
        object.__setattr__(
            self,
            "code",
            _enum_value(self.code, InferenceErrorCode, "code"),
        )
        object.__setattr__(
            self, "message", require_string(self.message, "message")
        )
        if not isinstance(self.retryable, bool):
            raise ContractValidationError("retryable", "must be a boolean")
        object.__setattr__(
            self,
            "details",
            normalize_json_object(self.details, "details"),
        )
        object.__setattr__(
            self,
            "request_id",
            optional_string(self.request_id, "request_id"),
        )

    def to_dict(self) -> dict[str, object]:
        data = {
            "schema_version": self.schema_version,
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
            "details": dict(self.details),
        }
        if self.request_id is not None:
            data["request_id"] = self.request_id
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "InferenceFailure":
        mapping = require_mapping(data, "error")
        retryable = mapping.get("retryable")
        if not isinstance(retryable, bool):
            raise ContractValidationError("retryable", "must be a boolean")
        return cls(
            schema_version=require_schema_version(
                mapping.get("schema_version")
            ),
            code=_enum_value(
                mapping.get("code"), InferenceErrorCode, "code"
            ),
            message=require_string(mapping.get("message"), "message"),
            retryable=retryable,
            details=normalize_json_object(
                mapping.get("details", {}), "details"
            ),
            request_id=optional_string(
                mapping.get("request_id"), "request_id"
            ),
        )


@dataclass(frozen=True, slots=True)
class InferenceRequest:
    """Explicit, idempotent request routed through an inference gateway."""

    request_id: str
    idempotency_key: str
    run_id: str
    stage_run_id: str
    task: InferenceTask
    model: RequestedModel
    inputs: Mapping[str, InferenceValue]
    parameters: Mapping[str, JSONValue]
    timeout_sec: float
    trace_id: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "schema_version",
            require_schema_version(self.schema_version),
        )
        for field_name in (
            "request_id",
            "idempotency_key",
            "run_id",
            "stage_run_id",
            "trace_id",
        ):
            object.__setattr__(
                self,
                field_name,
                require_string(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "task",
            _enum_value(self.task, InferenceTask, "task"),
        )
        if not isinstance(self.model, RequestedModel):
            raise ContractValidationError(
                "model", "must be a RequestedModel instance"
            )
        object.__setattr__(
            self, "inputs", _normalize_inference_values(self.inputs, "inputs")
        )
        object.__setattr__(
            self,
            "parameters",
            normalize_json_object(self.parameters, "parameters"),
        )
        object.__setattr__(
            self,
            "timeout_sec",
            require_number(self.timeout_sec, "timeout_sec", minimum=0.001),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "idempotency_key": self.idempotency_key,
            "run_id": self.run_id,
            "stage_run_id": self.stage_run_id,
            "task": self.task.value,
            "model": self.model.to_dict(),
            "inputs": _inference_values_to_dict(self.inputs),
            "parameters": dict(self.parameters),
            "timeout_sec": self.timeout_sec,
            "trace_id": self.trace_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "InferenceRequest":
        mapping = require_mapping(data, "inference_request")
        model = require_mapping(mapping.get("model"), "model")
        return cls(
            schema_version=require_schema_version(
                mapping.get("schema_version")
            ),
            request_id=require_string(
                mapping.get("request_id"), "request_id"
            ),
            idempotency_key=require_string(
                mapping.get("idempotency_key"), "idempotency_key"
            ),
            run_id=require_string(mapping.get("run_id"), "run_id"),
            stage_run_id=require_string(
                mapping.get("stage_run_id"), "stage_run_id"
            ),
            task=_enum_value(
                mapping.get("task"), InferenceTask, "task"
            ),
            model=RequestedModel.from_dict(model),
            inputs=_inference_values_from_dict(
                mapping.get("inputs", {}), "inputs"
            ),
            parameters=normalize_json_object(
                mapping.get("parameters", {}), "parameters"
            ),
            timeout_sec=require_number(
                mapping.get("timeout_sec"),
                "timeout_sec",
                minimum=0.001,
            ),
            trace_id=require_string(mapping.get("trace_id"), "trace_id"),
        )


@dataclass(frozen=True, slots=True)
class InferenceResponse:
    """Normalized terminal result from any inference provider."""

    request_id: str
    status: InferenceStatus
    outputs: Mapping[str, InferenceValue] = field(default_factory=dict)
    model: EffectiveModel | None = None
    usage: Mapping[str, JSONValue] = field(default_factory=dict)
    timing: Mapping[str, JSONValue] = field(default_factory=dict)
    warnings: Sequence[str] = ()
    error: InferenceFailure | None = None
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "schema_version",
            require_schema_version(self.schema_version),
        )
        object.__setattr__(
            self,
            "request_id",
            require_string(self.request_id, "request_id"),
        )
        status = _enum_value(self.status, InferenceStatus, "status")
        object.__setattr__(self, "status", status)
        object.__setattr__(
            self,
            "outputs",
            _normalize_inference_values(self.outputs, "outputs"),
        )
        if self.model is not None and not isinstance(
            self.model, EffectiveModel
        ):
            raise ContractValidationError(
                "model", "must be an EffectiveModel instance"
            )
        object.__setattr__(
            self, "usage", normalize_json_object(self.usage, "usage")
        )
        object.__setattr__(
            self, "timing", normalize_json_object(self.timing, "timing")
        )
        object.__setattr__(
            self,
            "warnings",
            normalize_string_tuple(self.warnings, "warnings", unique=False),
        )
        if self.error is not None and not isinstance(
            self.error, InferenceFailure
        ):
            raise ContractValidationError(
                "error", "must be an InferenceFailure instance"
            )
        if status is InferenceStatus.SUCCEEDED:
            if self.model is None:
                raise ContractValidationError(
                    "model", "is required for a successful response"
                )
            if self.error is not None:
                raise ContractValidationError(
                    "error", "must be omitted for a successful response"
                )
        elif self.error is None:
            raise ContractValidationError(
                "error", "is required for a non-success response"
            )
        if self.error is not None and self.error.request_id not in {
            None,
            self.request_id,
        }:
            raise ContractValidationError(
                "error.request_id", "must match response request_id"
            )
        if (
            status is InferenceStatus.CANCELLED
            and self.error is not None
            and self.error.code is not InferenceErrorCode.CANCELLED
        ):
            raise ContractValidationError(
                "error.code", "must be CANCELLED for a cancelled response"
            )

    def to_dict(self) -> dict[str, object]:
        data = {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "status": self.status.value,
            "outputs": _inference_values_to_dict(self.outputs),
            "usage": dict(self.usage),
            "timing": dict(self.timing),
            "warnings": list(self.warnings),
        }
        if self.model is not None:
            data["model"] = self.model.to_dict()
        if self.error is not None:
            data["error"] = self.error.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "InferenceResponse":
        mapping = require_mapping(data, "inference_response")
        raw_model = mapping.get("model")
        raw_error = mapping.get("error")
        return cls(
            schema_version=require_schema_version(
                mapping.get("schema_version")
            ),
            request_id=require_string(
                mapping.get("request_id"), "request_id"
            ),
            status=_enum_value(
                mapping.get("status"), InferenceStatus, "status"
            ),
            outputs=_inference_values_from_dict(
                mapping.get("outputs", {}), "outputs"
            ),
            model=(
                None
                if raw_model is None
                else EffectiveModel.from_dict(
                    require_mapping(raw_model, "model")
                )
            ),
            usage=normalize_json_object(mapping.get("usage", {}), "usage"),
            timing=normalize_json_object(
                mapping.get("timing", {}), "timing"
            ),
            warnings=normalize_string_tuple(
                mapping.get("warnings", ()), "warnings", unique=False
            ),
            error=(
                None
                if raw_error is None
                else InferenceFailure.from_dict(
                    require_mapping(raw_error, "error")
                )
            ),
        )


def _normalize_tasks(value: object) -> tuple[InferenceTask, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContractValidationError("tasks", "must be an array")
    tasks = tuple(
        _enum_value(task, InferenceTask, f"tasks[{index}]")
        for index, task in enumerate(value)
    )
    if len(tasks) != len(set(tasks)):
        raise ContractValidationError("tasks", "must not contain duplicates")
    return tasks


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """Static capability declaration used before routing a request."""

    provider: str
    tasks: Sequence[InferenceTask]
    model_aliases: Sequence[str]
    input_media_types: Sequence[str] = ()
    features: Sequence[str] = ()
    max_batch_size: int = 1
    max_artifact_bytes: int | None = None
    supports_cancellation: bool = False
    supports_async_jobs: bool = False
    effective_models: Mapping[str, EffectiveModel] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "schema_version",
            require_schema_version(self.schema_version),
        )
        object.__setattr__(
            self, "provider", require_string(self.provider, "provider")
        )
        object.__setattr__(self, "tasks", _normalize_tasks(self.tasks))
        object.__setattr__(
            self,
            "model_aliases",
            normalize_string_tuple(self.model_aliases, "model_aliases"),
        )
        object.__setattr__(
            self,
            "input_media_types",
            normalize_string_tuple(
                self.input_media_types, "input_media_types"
            ),
        )
        object.__setattr__(
            self,
            "features",
            normalize_string_tuple(self.features, "features"),
        )
        object.__setattr__(
            self,
            "max_batch_size",
            require_integer(
                self.max_batch_size, "max_batch_size", minimum=1
            ),
        )
        if self.max_artifact_bytes is not None:
            object.__setattr__(
                self,
                "max_artifact_bytes",
                require_integer(
                    self.max_artifact_bytes,
                    "max_artifact_bytes",
                    minimum=1,
                ),
            )
        for field_name in ("supports_cancellation", "supports_async_jobs"):
            if not isinstance(getattr(self, field_name), bool):
                raise ContractValidationError(
                    field_name, "must be a boolean"
                )
        effective_models = require_mapping(
            self.effective_models,
            "effective_models",
        )
        normalized_models = {}
        for alias, model in effective_models.items():
            normalized_alias = require_string(
                alias,
                "effective_models.key",
            )
            if normalized_alias not in self.model_aliases:
                raise ContractValidationError(
                    f"effective_models.{normalized_alias}",
                    "alias must be declared in model_aliases",
                )
            if not isinstance(model, EffectiveModel):
                raise ContractValidationError(
                    f"effective_models.{normalized_alias}",
                    "must be an EffectiveModel instance",
                )
            normalized_models[normalized_alias] = model
        object.__setattr__(self, "effective_models", normalized_models)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "provider": self.provider,
            "tasks": [task.value for task in self.tasks],
            "model_aliases": list(self.model_aliases),
            "input_media_types": list(self.input_media_types),
            "features": list(self.features),
            "max_batch_size": self.max_batch_size,
            "max_artifact_bytes": self.max_artifact_bytes,
            "supports_cancellation": self.supports_cancellation,
            "supports_async_jobs": self.supports_async_jobs,
            "effective_models": {
                alias: model.to_dict()
                for alias, model in self.effective_models.items()
            },
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ProviderCapabilities":
        mapping = require_mapping(data, "provider_capabilities")
        max_artifact_bytes = mapping.get("max_artifact_bytes")
        if max_artifact_bytes is not None:
            max_artifact_bytes = require_integer(
                max_artifact_bytes, "max_artifact_bytes", minimum=1
            )
        return cls(
            schema_version=require_schema_version(
                mapping.get("schema_version")
            ),
            provider=require_string(mapping.get("provider"), "provider"),
            tasks=_normalize_tasks(mapping.get("tasks", ())),
            model_aliases=normalize_string_tuple(
                mapping.get("model_aliases", ()), "model_aliases"
            ),
            input_media_types=normalize_string_tuple(
                mapping.get("input_media_types", ()), "input_media_types"
            ),
            features=normalize_string_tuple(
                mapping.get("features", ()), "features"
            ),
            max_batch_size=require_integer(
                mapping.get("max_batch_size", 1),
                "max_batch_size",
                minimum=1,
            ),
            max_artifact_bytes=max_artifact_bytes,
            supports_cancellation=mapping.get(
                "supports_cancellation", False
            ),
            supports_async_jobs=mapping.get("supports_async_jobs", False),
            effective_models={
                alias: EffectiveModel.from_dict(
                    require_mapping(
                        model,
                        f"effective_models.{alias}",
                    )
                )
                for alias, model in require_mapping(
                    mapping.get("effective_models", {}),
                    "effective_models",
                ).items()
            },
        )


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    """Provider health response used by readiness adapters."""

    provider: str
    status: HealthState
    details: Mapping[str, JSONValue] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "schema_version",
            require_schema_version(self.schema_version),
        )
        object.__setattr__(
            self, "provider", require_string(self.provider, "provider")
        )
        object.__setattr__(
            self,
            "status",
            _enum_value(self.status, HealthState, "status"),
        )
        object.__setattr__(
            self,
            "details",
            normalize_json_object(self.details, "details"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "provider": self.provider,
            "status": self.status.value,
            "details": dict(self.details),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ProviderHealth":
        mapping = require_mapping(data, "provider_health")
        return cls(
            schema_version=require_schema_version(
                mapping.get("schema_version")
            ),
            provider=require_string(mapping.get("provider"), "provider"),
            status=_enum_value(
                mapping.get("status"), HealthState, "status"
            ),
            details=normalize_json_object(
                mapping.get("details", {}), "details"
            ),
        )
