"""Versioned contracts for planning and reporting pipeline stages."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum

from ._validation import (
    JSONValue,
    SCHEMA_VERSION,
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


class StageStatus(str, Enum):
    """Terminal status returned by a Stage execution attempt."""

    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ResourceHints:
    """Advisory resources used by an Executor when placing a task."""

    cpu: float = 1.0
    memory_mb: int = 512
    gpu_optional: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "cpu",
            require_number(self.cpu, "resource_hints.cpu", minimum=0.1),
        )
        object.__setattr__(
            self,
            "memory_mb",
            require_integer(
                self.memory_mb, "resource_hints.memory_mb", minimum=1
            ),
        )
        if not isinstance(self.gpu_optional, bool):
            raise ContractValidationError(
                "resource_hints.gpu_optional", "must be a boolean"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "cpu": self.cpu,
            "memory_mb": self.memory_mb,
            "gpu_optional": self.gpu_optional,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ResourceHints":
        mapping = require_mapping(data, "resource_hints")
        return cls(
            cpu=require_number(
                mapping.get("cpu", 1.0), "resource_hints.cpu", minimum=0.1
            ),
            memory_mb=require_integer(
                mapping.get("memory_mb", 512),
                "resource_hints.memory_mb",
                minimum=1,
            ),
            gpu_optional=mapping.get("gpu_optional", False),
        )


@dataclass(frozen=True, slots=True)
class StageSpec:
    """Immutable registration metadata for one pipeline stage."""

    name: str
    stage_version: str
    dependencies: Sequence[str] = ()
    required_inputs: Sequence[str] = ()
    outputs: Sequence[str] = ()
    model_slots: Sequence[str] = ()
    resource_hints: ResourceHints = field(default_factory=ResourceHints)
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "schema_version",
            require_schema_version(self.schema_version),
        )
        name = require_string(self.name, "name")
        object.__setattr__(self, "name", name)
        object.__setattr__(
            self,
            "stage_version",
            require_string(self.stage_version, "stage_version"),
        )
        dependencies = normalize_string_tuple(
            self.dependencies, "dependencies"
        )
        if name in dependencies:
            raise ContractValidationError(
                "dependencies", "must not contain the stage itself"
            )
        object.__setattr__(self, "dependencies", dependencies)
        object.__setattr__(
            self,
            "required_inputs",
            normalize_string_tuple(self.required_inputs, "required_inputs"),
        )
        object.__setattr__(
            self,
            "outputs",
            normalize_string_tuple(self.outputs, "outputs"),
        )
        object.__setattr__(
            self,
            "model_slots",
            normalize_string_tuple(self.model_slots, "model_slots"),
        )
        if not isinstance(self.resource_hints, ResourceHints):
            raise ContractValidationError(
                "resource_hints", "must be a ResourceHints instance"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "stage_version": self.stage_version,
            "dependencies": list(self.dependencies),
            "required_inputs": list(self.required_inputs),
            "outputs": list(self.outputs),
            "model_slots": list(self.model_slots),
            "resource_hints": self.resource_hints.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "StageSpec":
        mapping = require_mapping(data, "stage_spec")
        hints = require_mapping(
            mapping.get("resource_hints", {}), "resource_hints"
        )
        return cls(
            schema_version=require_schema_version(
                mapping.get("schema_version")
            ),
            name=require_string(mapping.get("name"), "name"),
            stage_version=require_string(
                mapping.get("stage_version"), "stage_version"
            ),
            dependencies=normalize_string_tuple(
                mapping.get("dependencies", ()), "dependencies"
            ),
            required_inputs=normalize_string_tuple(
                mapping.get("required_inputs", ()), "required_inputs"
            ),
            outputs=normalize_string_tuple(
                mapping.get("outputs", ()), "outputs"
            ),
            model_slots=normalize_string_tuple(
                mapping.get("model_slots", ()), "model_slots"
            ),
            resource_hints=ResourceHints.from_dict(hints),
        )


def _normalize_artifact_mapping(
    value: Mapping[str, ArtifactRef] | object,
    field_name: str,
) -> dict[str, ArtifactRef]:
    mapping = require_mapping(value, field_name)
    artifacts = {}
    for key, artifact in mapping.items():
        require_string(key, f"{field_name}.key")
        if not isinstance(artifact, ArtifactRef):
            raise ContractValidationError(
                f"{field_name}.{key}", "must be an ArtifactRef instance"
            )
        artifacts[key] = artifact
    return artifacts


def _artifact_mapping_from_dict(
    value: object,
    field_name: str,
) -> dict[str, ArtifactRef]:
    mapping = require_mapping(value, field_name)
    return {
        key: ArtifactRef.from_dict(
            require_mapping(item, f"{field_name}.{key}")
        )
        for key, item in mapping.items()
    }


def _normalize_model_bindings(value: object) -> dict[str, str]:
    mapping = require_mapping(value, "model_bindings")
    return {
        require_string(key, "model_bindings.key"): require_string(
            binding, f"model_bindings.{key}"
        )
        for key, binding in mapping.items()
    }


@dataclass(frozen=True, slots=True)
class StageTask:
    """A fully explicit request to execute one stage attempt."""

    run_id: str
    stage_run_id: str
    attempt: int
    stage: str
    stage_version: str
    inputs: Mapping[str, ArtifactRef]
    config: Mapping[str, JSONValue]
    model_bindings: Mapping[str, str]
    idempotency_key: str
    trace_id: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "schema_version",
            require_schema_version(self.schema_version),
        )
        for field_name in (
            "run_id",
            "stage_run_id",
            "stage",
            "stage_version",
            "idempotency_key",
            "trace_id",
        ):
            object.__setattr__(
                self,
                field_name,
                require_string(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "attempt",
            require_integer(self.attempt, "attempt", minimum=1),
        )
        object.__setattr__(
            self,
            "inputs",
            _normalize_artifact_mapping(self.inputs, "inputs"),
        )
        object.__setattr__(
            self,
            "config",
            normalize_json_object(self.config, "config"),
        )
        object.__setattr__(
            self,
            "model_bindings",
            _normalize_model_bindings(self.model_bindings),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "stage_run_id": self.stage_run_id,
            "attempt": self.attempt,
            "stage": self.stage,
            "stage_version": self.stage_version,
            "inputs": {
                key: artifact.to_dict()
                for key, artifact in self.inputs.items()
            },
            "config": dict(self.config),
            "model_bindings": dict(self.model_bindings),
            "idempotency_key": self.idempotency_key,
            "trace_id": self.trace_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "StageTask":
        mapping = require_mapping(data, "stage_task")
        return cls(
            schema_version=require_schema_version(
                mapping.get("schema_version")
            ),
            run_id=require_string(mapping.get("run_id"), "run_id"),
            stage_run_id=require_string(
                mapping.get("stage_run_id"), "stage_run_id"
            ),
            attempt=require_integer(
                mapping.get("attempt"), "attempt", minimum=1
            ),
            stage=require_string(mapping.get("stage"), "stage"),
            stage_version=require_string(
                mapping.get("stage_version"), "stage_version"
            ),
            inputs=_artifact_mapping_from_dict(
                mapping.get("inputs", {}), "inputs"
            ),
            config=normalize_json_object(
                mapping.get("config", {}), "config"
            ),
            model_bindings=_normalize_model_bindings(
                mapping.get("model_bindings", {})
            ),
            idempotency_key=require_string(
                mapping.get("idempotency_key"), "idempotency_key"
            ),
            trace_id=require_string(mapping.get("trace_id"), "trace_id"),
        )


@dataclass(frozen=True, slots=True)
class ModelExecution:
    """Effective model/provider used by one model slot."""

    slot: str
    provider: str
    model: str
    revision: str
    runtime: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("slot", "provider", "model", "revision"):
            object.__setattr__(
                self,
                field_name,
                require_string(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self, "runtime", optional_string(self.runtime, "runtime")
        )

    def to_dict(self) -> dict[str, object]:
        data = {
            "slot": self.slot,
            "provider": self.provider,
            "model": self.model,
            "revision": self.revision,
        }
        if self.runtime is not None:
            data["runtime"] = self.runtime
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ModelExecution":
        mapping = require_mapping(data, "model_execution")
        return cls(
            slot=require_string(mapping.get("slot"), "slot"),
            provider=require_string(mapping.get("provider"), "provider"),
            model=require_string(mapping.get("model"), "model"),
            revision=require_string(mapping.get("revision"), "revision"),
            runtime=optional_string(mapping.get("runtime"), "runtime"),
        )


def _normalize_models(value: object) -> tuple[ModelExecution, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContractValidationError(
            "models", "must be an array of ModelExecution values"
        )
    models = tuple(value)
    for index, model in enumerate(models):
        if not isinstance(model, ModelExecution):
            raise ContractValidationError(
                f"models[{index}]", "must be a ModelExecution instance"
            )
    slots = [model.slot for model in models]
    if len(set(slots)) != len(slots):
        raise ContractValidationError(
            "models", "must not contain duplicate model slots"
        )
    return models


@dataclass(frozen=True, slots=True)
class StageResult:
    """Terminal result returned for one StageTask attempt."""

    run_id: str
    stage_run_id: str
    attempt: int
    status: StageStatus
    outputs: Mapping[str, ArtifactRef] = field(default_factory=dict)
    metrics: Mapping[str, JSONValue] = field(default_factory=dict)
    models: Sequence[ModelExecution] = ()
    warnings: Sequence[str] = ()
    reason_code: str | None = None
    reason: str | None = None
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "schema_version",
            require_schema_version(self.schema_version),
        )
        object.__setattr__(
            self, "run_id", require_string(self.run_id, "run_id")
        )
        object.__setattr__(
            self,
            "stage_run_id",
            require_string(self.stage_run_id, "stage_run_id"),
        )
        object.__setattr__(
            self,
            "attempt",
            require_integer(self.attempt, "attempt", minimum=1),
        )
        if isinstance(self.status, str):
            try:
                object.__setattr__(self, "status", StageStatus(self.status))
            except ValueError as exc:
                raise ContractValidationError(
                    "status", f"unknown terminal status {self.status!r}"
                ) from exc
        elif not isinstance(self.status, StageStatus):
            raise ContractValidationError(
                "status", "must be a StageStatus value"
            )
        object.__setattr__(
            self,
            "outputs",
            _normalize_artifact_mapping(self.outputs, "outputs"),
        )
        object.__setattr__(
            self,
            "metrics",
            normalize_json_object(self.metrics, "metrics"),
        )
        object.__setattr__(self, "models", _normalize_models(self.models))
        object.__setattr__(
            self,
            "warnings",
            normalize_string_tuple(
                self.warnings, "warnings", unique=False
            ),
        )
        object.__setattr__(
            self,
            "reason_code",
            optional_string(self.reason_code, "reason_code"),
        )
        object.__setattr__(
            self, "reason", optional_string(self.reason, "reason")
        )

    def to_dict(self) -> dict[str, object]:
        data = {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "stage_run_id": self.stage_run_id,
            "attempt": self.attempt,
            "status": self.status.value,
            "outputs": {
                key: artifact.to_dict()
                for key, artifact in self.outputs.items()
            },
            "metrics": dict(self.metrics),
            "models": [model.to_dict() for model in self.models],
            "warnings": list(self.warnings),
        }
        if self.reason_code is not None:
            data["reason_code"] = self.reason_code
        if self.reason is not None:
            data["reason"] = self.reason
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "StageResult":
        mapping = require_mapping(data, "stage_result")
        raw_models = mapping.get("models", [])
        if isinstance(raw_models, (str, bytes)) or not isinstance(
            raw_models, Sequence
        ):
            raise ContractValidationError("models", "must be an array")
        models = tuple(
            ModelExecution.from_dict(
                require_mapping(item, f"models[{index}]")
            )
            for index, item in enumerate(raw_models)
        )
        return cls(
            schema_version=require_schema_version(
                mapping.get("schema_version")
            ),
            run_id=require_string(mapping.get("run_id"), "run_id"),
            stage_run_id=require_string(
                mapping.get("stage_run_id"), "stage_run_id"
            ),
            attempt=require_integer(
                mapping.get("attempt"), "attempt", minimum=1
            ),
            status=require_string(mapping.get("status"), "status"),
            outputs=_artifact_mapping_from_dict(
                mapping.get("outputs", {}), "outputs"
            ),
            metrics=normalize_json_object(
                mapping.get("metrics", {}), "metrics"
            ),
            models=models,
            warnings=normalize_string_tuple(
                mapping.get("warnings", ()),
                "warnings",
                unique=False,
            ),
            reason_code=optional_string(
                mapping.get("reason_code"), "reason_code"
            ),
            reason=optional_string(mapping.get("reason"), "reason"),
        )
