"""Durable asynchronous pipeline-run application use cases."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from video_preprocess.domain import (
    ArtifactRef,
    RunStatus,
    StageStatus,
)
from video_preprocess.engine import PipelineRunResult
from video_preprocess.executors import CancellationToken
from video_preprocess.inference import InferenceDeploymentSettings
from video_preprocess.storage import LocalArtifactStore, LocalRunStore
from video_preprocess.storage._atomic import atomic_write_json

from .pipeline import (
    PipelineApplicationService,
    PipelineRunRequest,
    PipelineServiceInputError,
    PipelineSettings,
)


SCHEMA_VERSION = "1"


class PipelineRunServiceError(RuntimeError):
    """Base class for classified public run use-case failures."""


class PipelineRunNotFoundError(PipelineRunServiceError):
    """A requested public run does not exist."""


class PipelineIdempotencyConflictError(PipelineRunServiceError):
    """An idempotency key was reused for a different request."""


class PipelineRunNotReadyError(PipelineRunServiceError):
    """A result-only operation was requested before terminal state."""


class PipelineCapacityError(PipelineRunServiceError):
    """No additional local run can be admitted."""


class MediaNotFoundError(PipelineRunServiceError):
    """A media identifier cannot be safely resolved by the catalog."""


class PublicRunStatus(str, Enum):
    """Lifecycle exposed by the asynchronous public API."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {
            self.SUCCEEDED,
            self.FAILED,
            self.CANCELLED,
        }


@dataclass(frozen=True, slots=True)
class PipelineFailure:
    """Stable public failure data without implementation details."""

    code: str
    message: str
    retryable: bool = False
    stage: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "stage": self.stage,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "PipelineFailure":
        return cls(
            code=_required_text(data.get("code"), "failure.code"),
            message=_required_text(
                data.get("message"), "failure.message"
            ),
            retryable=_required_bool(
                data.get("retryable"), "failure.retryable"
            ),
            stage=_optional_text(data.get("stage"), "failure.stage"),
        )


@dataclass(frozen=True, slots=True)
class PipelineRunSubmission:
    """Transport-neutral public request after adapter validation."""

    idempotency_key: str
    media_id: str
    settings: PipelineSettings = field(default_factory=PipelineSettings)
    stage: str | None = None
    from_stage: str | None = None
    to_stage: str | None = None
    force_stages: Sequence[str] = ()
    stage_timeout_sec: float | None = None
    max_stage_attempts: int = 1
    retry_backoff_sec: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "idempotency_key",
            _required_text(self.idempotency_key, "idempotency_key"),
        )
        object.__setattr__(
            self,
            "media_id",
            _required_text(self.media_id, "media_id"),
        )
        if len(self.idempotency_key) > 200:
            raise ValueError("idempotency_key must be at most 200 characters")
        if len(self.media_id) > 500:
            raise ValueError("media_id must be at most 500 characters")
        if not isinstance(self.settings, PipelineSettings):
            raise TypeError("settings must be PipelineSettings")
        _validate_pipeline_settings(self.settings)
        for field_name in ("stage", "from_stage", "to_stage"):
            value = _optional_text(getattr(self, field_name), field_name)
            object.__setattr__(self, field_name, value)
        if self.stage is not None and (
            self.from_stage is not None or self.to_stage is not None
        ):
            raise ValueError(
                "stage cannot be combined with from_stage or to_stage"
            )
        if isinstance(self.force_stages, (str, bytes)):
            raise TypeError("force_stages must be a sequence")
        forced = tuple(
            _required_text(value, "force_stages")
            for value in self.force_stages
        )
        if len(set(forced)) != len(forced):
            raise ValueError("force_stages must not contain duplicates")
        object.__setattr__(self, "force_stages", forced)
        if self.stage_timeout_sec is not None:
            timeout = _positive_number(
                self.stage_timeout_sec,
                "stage_timeout_sec",
            )
            object.__setattr__(self, "stage_timeout_sec", timeout)
        if (
            isinstance(self.max_stage_attempts, bool)
            or not isinstance(self.max_stage_attempts, int)
            or not 1 <= self.max_stage_attempts <= 10
        ):
            raise ValueError("max_stage_attempts must be between 1 and 10")
        backoff = _non_negative_number(
            self.retry_backoff_sec,
            "retry_backoff_sec",
        )
        if backoff > 300:
            raise ValueError("retry_backoff_sec must be at most 300")
        object.__setattr__(self, "retry_backoff_sec", backoff)

    def fingerprint(self) -> str:
        payload = self.to_dict()
        payload.pop("idempotency_key")
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, object]:
        settings = {
            name: getattr(self.settings, name)
            for name in self.settings.__dataclass_fields__
        }
        return {
            "schema_version": SCHEMA_VERSION,
            "idempotency_key": self.idempotency_key,
            "media_id": self.media_id,
            "selection": {
                "stage": self.stage,
                "from_stage": self.from_stage,
                "to_stage": self.to_stage,
                "force_stages": list(self.force_stages),
            },
            "settings": settings,
            "execution_policy": {
                "stage_timeout_sec": self.stage_timeout_sec,
                "max_stage_attempts": self.max_stage_attempts,
                "retry_backoff_sec": self.retry_backoff_sec,
            },
        }


@dataclass(frozen=True, slots=True)
class PipelineRunSnapshot:
    """Durable API snapshot plus non-public idempotency metadata."""

    run_id: str
    status: PublicRunStatus
    created_at: str
    updated_at: str
    planned_stage_names: Sequence[str]
    completed_stage_names: Sequence[str] = ()
    current_stage: str | None = None
    current_attempt: int | None = None
    warnings: Sequence[str] = ()
    failure: PipelineFailure | None = None
    artifacts: Mapping[str, ArtifactRef] = field(default_factory=dict)
    completed_at: str | None = None
    idempotency_key: str = ""
    request_fingerprint: str = ""
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported pipeline run schema_version")
        object.__setattr__(self, "run_id", _required_text(self.run_id, "run_id"))
        if isinstance(self.status, str):
            object.__setattr__(self, "status", PublicRunStatus(self.status))
        elif not isinstance(self.status, PublicRunStatus):
            raise TypeError("status must be PublicRunStatus")
        for field_name in ("created_at", "updated_at"):
            _parse_timestamp(getattr(self, field_name), field_name)
        completed_at = _optional_text(self.completed_at, "completed_at")
        if self.status.terminal and completed_at is None:
            raise ValueError("terminal run requires completed_at")
        if not self.status.terminal and completed_at is not None:
            raise ValueError("non-terminal run cannot have completed_at")
        if completed_at is not None:
            _parse_timestamp(completed_at, "completed_at")
        object.__setattr__(self, "completed_at", completed_at)
        planned = _unique_text_tuple(
            self.planned_stage_names, "planned_stage_names"
        )
        completed = _unique_text_tuple(
            self.completed_stage_names, "completed_stage_names"
        )
        if not set(completed).issubset(planned):
            raise ValueError("completed stages must be planned")
        object.__setattr__(self, "planned_stage_names", planned)
        object.__setattr__(self, "completed_stage_names", completed)
        object.__setattr__(
            self,
            "current_stage",
            _optional_text(self.current_stage, "current_stage"),
        )
        if self.current_stage is not None and self.current_stage not in planned:
            raise ValueError("current_stage must be planned")
        if self.current_attempt is not None and (
            isinstance(self.current_attempt, bool)
            or not isinstance(self.current_attempt, int)
            or self.current_attempt < 1
        ):
            raise ValueError("current_attempt must be positive or None")
        warnings = tuple(
            _required_text(value, "warnings") for value in self.warnings
        )
        object.__setattr__(self, "warnings", warnings)
        if self.failure is not None and not isinstance(
            self.failure, PipelineFailure
        ):
            raise TypeError("failure must be PipelineFailure or None")
        artifacts = dict(self.artifacts)
        if not all(
            isinstance(name, str) and isinstance(ref, ArtifactRef)
            for name, ref in artifacts.items()
        ):
            raise TypeError("artifacts must map names to ArtifactRef values")
        object.__setattr__(self, "artifacts", artifacts)
        object.__setattr__(
            self,
            "idempotency_key",
            _required_text(self.idempotency_key, "idempotency_key"),
        )
        object.__setattr__(
            self,
            "request_fingerprint",
            _required_text(self.request_fingerprint, "request_fingerprint"),
        )

    @property
    def progress_ratio(self) -> float:
        if not self.planned_stage_names:
            return 1.0 if self.status is PublicRunStatus.SUCCEEDED else 0.0
        return len(self.completed_stage_names) / len(self.planned_stage_names)

    def public_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            **(
                {}
                if self.completed_at is None
                else {"completed_at": self.completed_at}
            ),
            "progress": {
                "planned_stages": len(self.planned_stage_names),
                "completed_stages": len(self.completed_stage_names),
                "ratio": self.progress_ratio,
                "current_stage": self.current_stage,
                "current_attempt": self.current_attempt,
            },
            "warnings": list(self.warnings),
            "failure": None if self.failure is None else self.failure.to_dict(),
            "links": {
                "self": f"/v1/pipeline-runs/{self.run_id}",
                "artifacts": (
                    f"/v1/pipeline-runs/{self.run_id}/artifacts"
                ),
                "queries": f"/v1/pipeline-runs/{self.run_id}/queries",
            },
        }

    def artifacts_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "artifacts": {
                name: ref.to_dict() for name, ref in self.artifacts.items()
            },
        }

    def record_dict(self) -> dict[str, object]:
        return {
            **self.public_dict(),
            "planned_stage_names": list(self.planned_stage_names),
            "completed_stage_names": list(self.completed_stage_names),
            "artifacts_snapshot": {
                name: ref.to_dict() for name, ref in self.artifacts.items()
            },
            "idempotency_key": self.idempotency_key,
            "request_fingerprint": self.request_fingerprint,
        }

    @classmethod
    def from_record_dict(
        cls,
        data: Mapping[str, object],
    ) -> "PipelineRunSnapshot":
        progress = _required_mapping(data.get("progress"), "progress")
        raw_failure = data.get("failure")
        raw_artifacts = _required_mapping(
            data.get("artifacts_snapshot", {}), "artifacts_snapshot"
        )
        return cls(
            schema_version=_required_text(
                data.get("schema_version"), "schema_version"
            ),
            run_id=_required_text(data.get("run_id"), "run_id"),
            status=_required_text(data.get("status"), "status"),
            created_at=_required_text(data.get("created_at"), "created_at"),
            updated_at=_required_text(data.get("updated_at"), "updated_at"),
            completed_at=_optional_text(
                data.get("completed_at"), "completed_at"
            ),
            planned_stage_names=_text_sequence(
                data.get("planned_stage_names"), "planned_stage_names"
            ),
            completed_stage_names=_text_sequence(
                data.get("completed_stage_names", ()),
                "completed_stage_names",
            ),
            current_stage=_optional_text(
                progress.get("current_stage"), "progress.current_stage"
            ),
            current_attempt=progress.get("current_attempt"),
            warnings=_text_sequence(data.get("warnings", ()), "warnings"),
            failure=(
                None
                if raw_failure is None
                else PipelineFailure.from_dict(
                    _required_mapping(raw_failure, "failure")
                )
            ),
            artifacts={
                name: ArtifactRef.from_dict(
                    _required_mapping(raw, f"artifacts_snapshot.{name}")
                )
                for name, raw in raw_artifacts.items()
            },
            idempotency_key=_required_text(
                data.get("idempotency_key"), "idempotency_key"
            ),
            request_fingerprint=_required_text(
                data.get("request_fingerprint"), "request_fingerprint"
            ),
        )


class PipelineRunRepository(Protocol):
    def save(self, snapshot: PipelineRunSnapshot) -> None: ...

    def load(self, run_id: str) -> PipelineRunSnapshot | None: ...

    def find_by_idempotency_key(
        self, idempotency_key: str
    ) -> PipelineRunSnapshot | None: ...

    def list(self) -> Sequence[PipelineRunSnapshot]: ...


class LocalPipelineRunRepository:
    """Atomic JSON repository for public run and idempotency snapshots."""

    def __init__(self, root: Path) -> None:
        configured_root = Path(root)
        if configured_root.is_symlink():
            raise ValueError("pipeline run repository must not be a symlink")
        self.root = configured_root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def save(self, snapshot: PipelineRunSnapshot) -> None:
        if not isinstance(snapshot, PipelineRunSnapshot):
            raise TypeError("snapshot must be PipelineRunSnapshot")
        with self._lock:
            atomic_write_json(
                self._path(snapshot.run_id), snapshot.record_dict()
            )

    def load(self, run_id: str) -> PipelineRunSnapshot | None:
        path = self._path(run_id)
        with self._lock:
            if not path.is_file():
                return None
            payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("pipeline run record must be an object")
        snapshot = PipelineRunSnapshot.from_record_dict(payload)
        if snapshot.run_id != run_id:
            raise ValueError("pipeline run ID does not match record path")
        return snapshot

    def find_by_idempotency_key(
        self, idempotency_key: str
    ) -> PipelineRunSnapshot | None:
        normalized = _required_text(idempotency_key, "idempotency_key")
        for snapshot in self.list():
            if snapshot.idempotency_key == normalized:
                return snapshot
        return None

    def list(self) -> tuple[PipelineRunSnapshot, ...]:
        with self._lock:
            paths = tuple(sorted(self.root.glob("run-*.json")))
        snapshots = []
        for path in paths:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("pipeline run record must be an object")
            snapshots.append(PipelineRunSnapshot.from_record_dict(payload))
        return tuple(snapshots)

    def _path(self, run_id: str) -> Path:
        normalized = _required_text(run_id, "run_id")
        path = self.root / f"run-{quote(normalized, safe='-._~')}.json"
        try:
            path.resolve(strict=False).relative_to(self.root)
        except ValueError as exc:
            raise ValueError("pipeline run record path escapes root") from exc
        return path


@dataclass(frozen=True, slots=True)
class EngineRunObservation:
    """Read-only projection of Engine manifests for live API progress."""

    status: RunStatus
    stage_results: Sequence[tuple[str, int, StageStatus]]
    warnings: Sequence[str]
    artifacts: Mapping[str, ArtifactRef]
    failure: PipelineFailure | None = None


class PipelineProgressReader(Protocol):
    def read(self, run_id: str) -> EngineRunObservation | None: ...


class LocalPipelineProgressReader:
    """Project Engine run/stage manifests without exposing their paths."""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = Path(workspace_root).resolve()

    def read(self, run_id: str) -> EngineRunObservation | None:
        root = self._run_root(run_id)
        if not root.is_dir():
            return None
        artifacts = LocalArtifactStore(
            root, namespace="api-progress-reader", read_only=True
        )
        store = LocalRunStore(root, artifacts, read_only=True)
        manifest = store.load_run(run_id)
        if manifest is None:
            return None
        results = []
        warnings = list(manifest.warnings)
        outputs: dict[str, ArtifactRef] = {}
        failure = None
        for reference in manifest.stages:
            stage = store.load_stage(run_id, reference)
            if stage is None:
                continue
            result = stage.result
            results.append((stage.task.stage, stage.task.attempt, result.status))
            warnings.extend(result.warnings)
            outputs.update(result.outputs)
            if result.status in {StageStatus.FAILED, StageStatus.CANCELLED}:
                failure = PipelineFailure(
                    code=(
                        "CANCELLED"
                        if result.status is StageStatus.CANCELLED
                        else "PIPELINE_FAILED"
                    ),
                    message=result.reason or "pipeline stage did not succeed",
                    retryable=False,
                    stage=stage.task.stage,
                )
        return EngineRunObservation(
            status=manifest.status,
            stage_results=tuple(results),
            warnings=tuple(warnings),
            artifacts=outputs,
            failure=failure,
        )

    def _run_root(self, run_id: str) -> Path:
        root = (self.workspace_root / _required_text(run_id, "run_id")).resolve()
        try:
            root.relative_to(self.workspace_root)
        except ValueError as exc:
            raise ValueError("run workspace escapes configured root") from exc
        return root


class LocalMediaCatalog:
    """Resolve media IDs only inside one configured local root."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ValueError("media catalog root must be an existing directory")

    def resolve(self, media_id: str) -> Path:
        normalized = _required_text(media_id, "media_id")
        if "\x00" in normalized or Path(normalized).is_absolute():
            raise MediaNotFoundError("media_id is not available")
        candidate = (self.root / normalized).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise MediaNotFoundError("media_id is not available") from exc
        if not candidate.is_file():
            raise MediaNotFoundError("media_id is not available")
        return candidate


class PipelineRunService:
    """Create, observe and cancel durable local pipeline jobs."""

    def __init__(
        self,
        application: PipelineApplicationService,
        repository: PipelineRunRepository,
        media_catalog: LocalMediaCatalog,
        workspace_root: Path,
        *,
        deployments: InferenceDeploymentSettings | None = None,
        progress_reader: PipelineProgressReader | None = None,
        max_active_runs: int = 1,
        run_id_factory: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        for method_name in ("plan", "run"):
            if not callable(getattr(application, method_name, None)):
                raise TypeError(f"application must implement {method_name}")
        for method_name in ("save", "load", "find_by_idempotency_key", "list"):
            if not callable(getattr(repository, method_name, None)):
                raise TypeError(f"repository must implement {method_name}")
        if not isinstance(media_catalog, LocalMediaCatalog):
            raise TypeError("media_catalog must be LocalMediaCatalog")
        if (
            isinstance(max_active_runs, bool)
            or not isinstance(max_active_runs, int)
            or max_active_runs < 1
        ):
            raise ValueError("max_active_runs must be positive")
        self.application = application
        self.repository = repository
        self.media_catalog = media_catalog
        self.workspace_root = Path(workspace_root).resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.deployments = deployments or InferenceDeploymentSettings()
        if not isinstance(self.deployments, InferenceDeploymentSettings):
            raise TypeError("deployments must be InferenceDeploymentSettings")
        self.progress_reader = progress_reader or LocalPipelineProgressReader(
            self.workspace_root
        )
        if not callable(getattr(self.progress_reader, "read", None)):
            raise TypeError("progress_reader must implement read")
        self.max_active_runs = max_active_runs
        self.run_id_factory = run_id_factory or (
            lambda: f"run_{uuid.uuid4().hex}"
        )
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancellations: dict[str, CancellationToken] = {}
        self._reconcile_interrupted_runs()

    async def create(
        self, submission: PipelineRunSubmission
    ) -> tuple[PipelineRunSnapshot, bool]:
        if not isinstance(submission, PipelineRunSubmission):
            raise TypeError("submission must be PipelineRunSubmission")
        existing = self.repository.find_by_idempotency_key(
            submission.idempotency_key
        )
        fingerprint = submission.fingerprint()
        if existing is not None:
            if existing.request_fingerprint != fingerprint:
                raise PipelineIdempotencyConflictError(
                    "idempotency key is already bound to another request"
                )
            return self._refresh(existing), False
        active_count = sum(
            not snapshot.status.terminal for snapshot in self.repository.list()
        )
        if active_count >= self.max_active_runs:
            raise PipelineCapacityError("pipeline run capacity is exhausted")

        media_path = self.media_catalog.resolve(submission.media_id)
        run_id = _required_text(self.run_id_factory(), "run_id")
        output_root = self._output_root(run_id)
        request = PipelineRunRequest(
            video_path=media_path,
            output_root=output_root,
            settings=submission.settings,
            deployments=self.deployments,
            run_id=run_id,
            stage=submission.stage,
            from_stage=submission.from_stage,
            to_stage=submission.to_stage,
            force_stages=submission.force_stages,
            stage_timeout_sec=submission.stage_timeout_sec,
            max_stage_attempts=submission.max_stage_attempts,
            retry_backoff_sec=submission.retry_backoff_sec,
        )
        plan = self.application.plan(request)
        now = self._now()
        snapshot = PipelineRunSnapshot(
            run_id=run_id,
            status=PublicRunStatus.QUEUED,
            created_at=now,
            updated_at=now,
            planned_stage_names=plan.stage_names,
            current_stage=plan.stage_names[0] if plan.stage_names else None,
            current_attempt=1 if plan.stage_names else None,
            idempotency_key=submission.idempotency_key,
            request_fingerprint=fingerprint,
        )
        self.repository.save(snapshot)
        cancellation = CancellationToken()
        self._cancellations[run_id] = cancellation
        task = asyncio.create_task(
            self._execute(snapshot, request, cancellation),
            name=f"pipeline-run:{run_id}",
        )
        self._tasks[run_id] = task
        task.add_done_callback(lambda _: self._release(run_id))
        return snapshot, True

    def get(self, run_id: str) -> PipelineRunSnapshot:
        snapshot = self.repository.load(run_id)
        if snapshot is None:
            raise PipelineRunNotFoundError("pipeline run was not found")
        return self._refresh(snapshot)

    async def cancel(self, run_id: str) -> PipelineRunSnapshot:
        snapshot = self.get(run_id)
        if snapshot.status.terminal:
            return snapshot
        cancellation = self._cancellations.get(run_id)
        if cancellation is None:
            return self._mark_interrupted(snapshot)
        cancellation.cancel()
        return self.get(run_id)

    def artifacts(self, run_id: str) -> dict[str, object]:
        snapshot = self.get(run_id)
        if not snapshot.status.terminal:
            raise PipelineRunNotReadyError(
                "pipeline artifacts are available after terminal state"
            )
        return snapshot.artifacts_dict()

    async def wait(self, run_id: str) -> PipelineRunSnapshot:
        task = self._tasks.get(run_id)
        if task is not None:
            await asyncio.shield(task)
        return self.get(run_id)

    async def _execute(
        self,
        snapshot: PipelineRunSnapshot,
        request: PipelineRunRequest,
        cancellation: CancellationToken,
    ) -> None:
        running = replace(
            snapshot,
            status=PublicRunStatus.RUNNING,
            updated_at=self._now(),
        )
        self.repository.save(running)
        try:
            result = await self.application.run(
                request,
                cancellation=cancellation,
            )
        except PipelineServiceInputError:
            self._save_failure(
                running,
                PipelineFailure(
                    "INVALID_REQUEST",
                    "pipeline request could not be executed",
                    False,
                ),
            )
        except Exception as exc:
            self._save_failure(
                running,
                PipelineFailure(
                    "INTERNAL",
                    "pipeline execution failed",
                    False,
                ),
                warning=f"error_type={type(exc).__name__}",
            )
        else:
            self._save_result(running, result)

    def _save_result(
        self,
        snapshot: PipelineRunSnapshot,
        result: PipelineRunResult,
    ) -> None:
        status = PublicRunStatus(result.status.value)
        completed = tuple(dict.fromkeys(record.stage for record in result.stages))
        warnings = tuple(
            warning
            for record in result.stages
            for warning in record.result.warnings
        )
        failure = None
        if status in {PublicRunStatus.FAILED, PublicRunStatus.CANCELLED}:
            final = result.stages[-1] if result.stages else None
            failure = PipelineFailure(
                code=(
                    "CANCELLED"
                    if status is PublicRunStatus.CANCELLED
                    else "PIPELINE_FAILED"
                ),
                message=(
                    final.result.reason
                    if final is not None and final.result.reason is not None
                    else (
                        "pipeline was cancelled"
                        if status is PublicRunStatus.CANCELLED
                        else "pipeline stage did not succeed"
                    )
                ),
                retryable=False,
                stage=None if final is None else final.stage,
            )
        now = self._now()
        self.repository.save(
            replace(
                snapshot,
                status=status,
                updated_at=now,
                completed_at=now,
                completed_stage_names=completed,
                current_stage=None,
                current_attempt=None,
                warnings=warnings,
                failure=failure,
                artifacts={
                    name: ref
                    for name, ref in result.artifacts.items()
                    if name != "video"
                },
            )
        )

    def _save_failure(
        self,
        snapshot: PipelineRunSnapshot,
        failure: PipelineFailure,
        *,
        warning: str | None = None,
    ) -> None:
        now = self._now()
        warnings = snapshot.warnings + (() if warning is None else (warning,))
        self.repository.save(
            replace(
                snapshot,
                status=PublicRunStatus.FAILED,
                updated_at=now,
                completed_at=now,
                current_stage=None,
                current_attempt=None,
                warnings=warnings,
                failure=failure,
            )
        )

    def _refresh(self, snapshot: PipelineRunSnapshot) -> PipelineRunSnapshot:
        if snapshot.status.terminal:
            return snapshot
        if snapshot.run_id not in self._tasks:
            return self._mark_interrupted(snapshot)
        observation = self.progress_reader.read(snapshot.run_id)
        if observation is None:
            return snapshot
        successful = tuple(
            dict.fromkeys(
                stage
                for stage, _, status in observation.stage_results
                if status in {StageStatus.SUCCEEDED, StageStatus.SKIPPED}
            )
        )
        current = next(
            (
                stage
                for stage in snapshot.planned_stage_names
                if stage not in successful
            ),
            None,
        )
        attempt = None
        if current is not None:
            prior_attempts = [
                number
                for stage, number, _ in observation.stage_results
                if stage == current
            ]
            attempt = max(prior_attempts, default=0) + 1
        status = PublicRunStatus(observation.status.value)
        completed = successful
        completed_at = None
        failure = None
        if status.terminal:
            completed = tuple(
                dict.fromkeys(stage for stage, _, _ in observation.stage_results)
            )
            completed_at = self._now()
            current = None
            attempt = None
            failure = observation.failure
        refreshed = replace(
            snapshot,
            status=status,
            updated_at=self._now(),
            completed_at=completed_at,
            completed_stage_names=completed,
            current_stage=current,
            current_attempt=attempt,
            warnings=tuple(observation.warnings),
            failure=failure,
            artifacts=dict(observation.artifacts),
        )
        self.repository.save(refreshed)
        return refreshed

    def _reconcile_interrupted_runs(self) -> None:
        for snapshot in self.repository.list():
            if not snapshot.status.terminal:
                self._mark_interrupted(snapshot)

    def _mark_interrupted(
        self, snapshot: PipelineRunSnapshot
    ) -> PipelineRunSnapshot:
        now = self._now()
        interrupted = replace(
            snapshot,
            status=PublicRunStatus.FAILED,
            updated_at=now,
            completed_at=now,
            current_stage=None,
            current_attempt=None,
            failure=PipelineFailure(
                "RUN_INTERRUPTED",
                "pipeline execution was interrupted by process restart",
                True,
            ),
        )
        self.repository.save(interrupted)
        return interrupted

    def _output_root(self, run_id: str) -> Path:
        root = (self.workspace_root / run_id).resolve()
        try:
            root.relative_to(self.workspace_root)
        except ValueError as exc:
            raise ValueError("run workspace escapes configured root") from exc
        return root

    def _now(self) -> str:
        value = self.clock()
        if not isinstance(value, datetime):
            raise TypeError("clock must return datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _release(self, run_id: str) -> None:
        self._tasks.pop(run_id, None)
        self._cancellations.pop(run_id, None)


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name)


def _required_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a boolean")
    return value


def _positive_number(value: object, field_name: str) -> float:
    number = _non_negative_number(value, field_name)
    if number <= 0:
        raise ValueError(f"{field_name} must be positive")
    return number


def _non_negative_number(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return number


def _validate_pipeline_settings(settings: PipelineSettings) -> None:
    for field_name in (
        "scene_threshold",
        "stt_merge_gap_sec",
    ):
        _non_negative_number(getattr(settings, field_name), field_name)
    for field_name in (
        "min_scene_len_frames",
        "keyframes_per_scene",
    ):
        value = getattr(settings, field_name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{field_name} must be a positive integer")
    for field_name in ("vad_min_silence_ms", "vad_speech_pad_ms"):
        value = getattr(settings, field_name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{field_name} must be a non-negative integer")
    for field_name in (
        "whisper_model",
        "caption_model",
        "embed_model",
        "diarize_model",
    ):
        _required_text(getattr(settings, field_name), field_name)
    _optional_text(settings.language, "language")


def _required_mapping(
    value: object, field_name: str
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise ValueError(f"{field_name} must use string keys")
    return value


def _text_sequence(value: object, field_name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{field_name} must be an array")
    return tuple(_required_text(item, field_name) for item in value)


def _unique_text_tuple(value: object, field_name: str) -> tuple[str, ...]:
    items = _text_sequence(value, field_name)
    if len(set(items)) != len(items):
        raise ValueError(f"{field_name} must not contain duplicates")
    return items


def _parse_timestamp(value: object, field_name: str) -> datetime:
    text = _required_text(value, field_name)
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a UTC offset")
    return parsed
