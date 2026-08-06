"""Versioned manifests persisted by a Run Store."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from ._validation import (
    JSONValue,
    SCHEMA_VERSION,
    normalize_json_object,
    normalize_string_tuple,
    optional_string,
    require_integer,
    require_mapping,
    require_schema_version,
    require_string,
)
from .artifacts import ArtifactRef
from .errors import ContractValidationError
from .stages import StageResult, StageTask


class RunStatus(str, Enum):
    """Lifecycle status persisted for one pipeline run."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_RUN_STATUSES = {
    RunStatus.SUCCEEDED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
}


def _normalize_run_status(value: RunStatus | str) -> RunStatus:
    if isinstance(value, RunStatus):
        return value
    if isinstance(value, str):
        try:
            return RunStatus(value)
        except ValueError as exc:
            raise ContractValidationError(
                "status", f"unknown run status {value!r}"
            ) from exc
    raise ContractValidationError("status", "must be a RunStatus value")


def _parse_timestamp(value: object, field_name: str) -> tuple[str, datetime]:
    text = require_string(value, field_name)
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ContractValidationError(
            field_name, "must be an ISO 8601 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractValidationError(
            field_name, "must include a UTC offset"
        )
    return text, parsed


def _normalize_artifacts(
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


def _artifacts_from_dict(
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


def _normalize_bindings(value: object) -> dict[str, str]:
    mapping = require_mapping(value, "model_bindings")
    return {
        require_string(key, "model_bindings.key"): require_string(
            binding, f"model_bindings.{key}"
        )
        for key, binding in mapping.items()
    }


@dataclass(frozen=True, slots=True)
class StageAttemptRef:
    """Reference to one persisted Stage attempt manifest."""

    stage_run_id: str
    attempt: int

    def __post_init__(self) -> None:
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

    def to_dict(self) -> dict[str, object]:
        return {
            "stage_run_id": self.stage_run_id,
            "attempt": self.attempt,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "StageAttemptRef":
        mapping = require_mapping(data, "stage_attempt")
        return cls(
            stage_run_id=require_string(
                mapping.get("stage_run_id"), "stage_run_id"
            ),
            attempt=require_integer(
                mapping.get("attempt"), "attempt", minimum=1
            ),
        )


def _normalize_stage_attempts(value: object) -> tuple[StageAttemptRef, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContractValidationError(
            "stages", "must be an array of StageAttemptRef values"
        )
    attempts = tuple(value)
    keys = []
    for index, attempt in enumerate(attempts):
        if not isinstance(attempt, StageAttemptRef):
            raise ContractValidationError(
                f"stages[{index}]", "must be a StageAttemptRef instance"
            )
        keys.append((attempt.stage_run_id, attempt.attempt))
    if len(keys) != len(set(keys)):
        raise ContractValidationError("stages", "must not contain duplicates")
    return attempts


@dataclass(frozen=True, slots=True)
class RunManifest:
    """Run-level state and references to persisted Stage attempts."""

    run_id: str
    status: RunStatus
    started_at: str
    updated_at: str
    input_artifacts: Mapping[str, ArtifactRef] = field(default_factory=dict)
    config: Mapping[str, JSONValue] = field(default_factory=dict)
    model_bindings: Mapping[str, str] = field(default_factory=dict)
    stages: Sequence[StageAttemptRef] = ()
    warnings: Sequence[str] = ()
    completed_at: str | None = None
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
        status = _normalize_run_status(self.status)
        object.__setattr__(self, "status", status)
        started_at, started = _parse_timestamp(
            self.started_at, "started_at"
        )
        updated_at, updated = _parse_timestamp(
            self.updated_at, "updated_at"
        )
        if updated < started:
            raise ContractValidationError(
                "updated_at", "must not be before started_at"
            )
        object.__setattr__(self, "started_at", started_at)
        object.__setattr__(self, "updated_at", updated_at)

        completed_at = optional_string(self.completed_at, "completed_at")
        if status in TERMINAL_RUN_STATUSES:
            if completed_at is None:
                raise ContractValidationError(
                    "completed_at", "is required for a terminal run"
                )
            completed_at, completed = _parse_timestamp(
                completed_at, "completed_at"
            )
            if completed < started:
                raise ContractValidationError(
                    "completed_at", "must not be before started_at"
                )
            if updated < completed:
                raise ContractValidationError(
                    "updated_at", "must not be before completed_at"
                )
        elif completed_at is not None:
            raise ContractValidationError(
                "completed_at", "must be omitted for a non-terminal run"
            )
        object.__setattr__(self, "completed_at", completed_at)
        object.__setattr__(
            self,
            "input_artifacts",
            _normalize_artifacts(self.input_artifacts, "input_artifacts"),
        )
        object.__setattr__(
            self, "config", normalize_json_object(self.config, "config")
        )
        object.__setattr__(
            self,
            "model_bindings",
            _normalize_bindings(self.model_bindings),
        )
        object.__setattr__(
            self, "stages", _normalize_stage_attempts(self.stages)
        )
        object.__setattr__(
            self,
            "warnings",
            normalize_string_tuple(self.warnings, "warnings", unique=False),
        )

    def to_dict(self) -> dict[str, object]:
        data = {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "status": self.status.value,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "input_artifacts": {
                key: artifact.to_dict()
                for key, artifact in self.input_artifacts.items()
            },
            "config": dict(self.config),
            "model_bindings": dict(self.model_bindings),
            "stages": [stage.to_dict() for stage in self.stages],
            "warnings": list(self.warnings),
        }
        if self.completed_at is not None:
            data["completed_at"] = self.completed_at
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "RunManifest":
        mapping = require_mapping(data, "run_manifest")
        raw_stages = mapping.get("stages", [])
        if isinstance(raw_stages, (str, bytes)) or not isinstance(
            raw_stages, Sequence
        ):
            raise ContractValidationError("stages", "must be an array")
        stages = tuple(
            StageAttemptRef.from_dict(
                require_mapping(item, f"stages[{index}]")
            )
            for index, item in enumerate(raw_stages)
        )
        return cls(
            schema_version=require_schema_version(
                mapping.get("schema_version")
            ),
            run_id=require_string(mapping.get("run_id"), "run_id"),
            status=require_string(mapping.get("status"), "status"),
            started_at=require_string(
                mapping.get("started_at"), "started_at"
            ),
            updated_at=require_string(
                mapping.get("updated_at"), "updated_at"
            ),
            input_artifacts=_artifacts_from_dict(
                mapping.get("input_artifacts", {}), "input_artifacts"
            ),
            config=normalize_json_object(
                mapping.get("config", {}), "config"
            ),
            model_bindings=_normalize_bindings(
                mapping.get("model_bindings", {})
            ),
            stages=stages,
            warnings=normalize_string_tuple(
                mapping.get("warnings", ()), "warnings", unique=False
            ),
            completed_at=optional_string(
                mapping.get("completed_at"), "completed_at"
            ),
        )


@dataclass(frozen=True, slots=True)
class StageManifest:
    """Task, terminal result, and timing for one Stage attempt."""

    task: StageTask
    result: StageResult
    started_at: str
    completed_at: str
    cache_key: str | None = None
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "schema_version",
            require_schema_version(self.schema_version),
        )
        if not isinstance(self.task, StageTask):
            raise ContractValidationError("task", "must be a StageTask")
        if not isinstance(self.result, StageResult):
            raise ContractValidationError("result", "must be a StageResult")
        for field_name in ("run_id", "stage_run_id", "attempt"):
            if getattr(self.task, field_name) != getattr(
                self.result, field_name
            ):
                raise ContractValidationError(
                    "result", f"{field_name} must match task"
                )
        started_at, started = _parse_timestamp(
            self.started_at, "started_at"
        )
        completed_at, completed = _parse_timestamp(
            self.completed_at, "completed_at"
        )
        if completed < started:
            raise ContractValidationError(
                "completed_at", "must not be before started_at"
            )
        object.__setattr__(self, "started_at", started_at)
        object.__setattr__(self, "completed_at", completed_at)
        object.__setattr__(
            self, "cache_key", optional_string(self.cache_key, "cache_key")
        )

    @property
    def reference(self) -> StageAttemptRef:
        return StageAttemptRef(
            stage_run_id=self.task.stage_run_id,
            attempt=self.task.attempt,
        )

    def to_dict(self) -> dict[str, object]:
        data = {
            "schema_version": self.schema_version,
            "task": self.task.to_dict(),
            "result": self.result.to_dict(),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }
        if self.cache_key is not None:
            data["cache_key"] = self.cache_key
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "StageManifest":
        mapping = require_mapping(data, "stage_manifest")
        task = require_mapping(mapping.get("task"), "task")
        result = require_mapping(mapping.get("result"), "result")
        return cls(
            schema_version=require_schema_version(
                mapping.get("schema_version")
            ),
            task=StageTask.from_dict(task),
            result=StageResult.from_dict(result),
            started_at=require_string(
                mapping.get("started_at"), "started_at"
            ),
            completed_at=require_string(
                mapping.get("completed_at"), "completed_at"
            ),
            cache_key=optional_string(
                mapping.get("cache_key"), "cache_key"
            ),
        )
