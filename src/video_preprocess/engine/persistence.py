"""RunStore-backed journal for PipelineEngine state transitions."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from video_preprocess.domain import (
    ArtifactRef,
    RunManifest,
    RunStatus,
    StageAttemptRef,
    StageManifest,
    StageResult,
    StageTask,
)

from .errors import EnginePersistenceError

if TYPE_CHECKING:
    from video_preprocess.storage.runs import RunStore


Clock = Callable[[], datetime]


def utc_now() -> datetime:
    """Return an aware UTC timestamp for persisted manifests."""

    return datetime.now(timezone.utc)


class RunJournal:
    """Persist one PipelineEngine run without owning execution policy."""

    def __init__(
        self,
        run_store: RunStore,
        *,
        run_id: str,
        input_artifacts: Mapping[str, ArtifactRef],
        stage_configs: Mapping[str, Mapping[str, object]],
        model_bindings: Mapping[str, Mapping[str, object]],
        stage_order: Sequence[str] = (),
        clock: Clock = utc_now,
    ) -> None:
        for method_name in (
            "save_run",
            "load_stage",
            "save_stage",
            "find_stages_by_cache_key",
        ):
            if not callable(getattr(run_store, method_name, None)):
                raise TypeError("run_store must implement the RunStore Port")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.run_store = run_store
        self.run_id = run_id
        self.input_artifacts = dict(input_artifacts)
        self.config = {
            stage: dict(config)
            for stage, config in stage_configs.items()
        }
        self.model_bindings = {
            f"{stage}.{slot}": binding
            for stage, bindings in model_bindings.items()
            for slot, binding in bindings.items()
        }
        self.clock = clock
        self.stage_order = {
            stage_name: index for index, stage_name in enumerate(stage_order)
        }
        if len(self.stage_order) != len(tuple(stage_order)):
            raise ValueError("stage_order must not contain duplicates")
        self.started_at: str | None = None
        self.stage_references: list[StageAttemptRef] = []
        self._reference_order: dict[StageAttemptRef, tuple[int, int]] = {}

    def now(self) -> str:
        """Read and normalize one injected clock value."""

        try:
            value = self.clock()
        except Exception as exc:
            raise EnginePersistenceError("run clock failed") from exc
        if not isinstance(value, datetime):
            raise EnginePersistenceError("run clock must return datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise EnginePersistenceError(
                "run clock must return a timezone-aware datetime"
            )
        return value.astimezone(timezone.utc).isoformat().replace(
            "+00:00",
            "Z",
        )

    def start(self) -> RunManifest:
        """Persist the initial running manifest."""

        if self.started_at is not None:
            raise EnginePersistenceError("run journal is already started")
        self.started_at = self.now()
        manifest = self._build_run_manifest(
            RunStatus.RUNNING,
            updated_at=self.started_at,
        )
        self._save_run(manifest)
        return manifest

    def load_candidate(self, task: StageTask) -> StageManifest | None:
        """Load the same run/stage attempt as a resumable cache candidate."""

        self._require_started()
        reference = StageAttemptRef(task.stage_run_id, task.attempt)
        try:
            return self.run_store.load_stage(self.run_id, reference)
        except Exception as exc:
            raise EnginePersistenceError(
                "could not load a Stage cache candidate"
            ) from exc

    def record_stage(
        self,
        task: StageTask,
        result: StageResult,
        *,
        started_at: str,
        completed_at: str,
        cache_key: str,
    ) -> StageManifest:
        """Persist a terminal Stage then update the running run manifest."""

        self._require_started()
        manifest = StageManifest(
            task=task,
            result=result,
            started_at=started_at,
            completed_at=completed_at,
            cache_key=cache_key,
        )
        try:
            self.run_store.save_stage(manifest)
        except Exception as exc:
            raise EnginePersistenceError(
                "could not save a Stage manifest"
            ) from exc
        if manifest.reference not in self.stage_references:
            self.stage_references.append(manifest.reference)
        self._reference_order[manifest.reference] = (
            self.stage_order.get(task.stage, len(self.stage_order)),
            task.attempt,
        )
        self.stage_references.sort(
            key=lambda reference: self._reference_order[reference]
        )
        self._save_run(
            self._build_run_manifest(
                RunStatus.RUNNING,
                updated_at=completed_at,
            )
        )
        return manifest

    def finish(self, status: RunStatus) -> RunManifest:
        """Persist and return one terminal run manifest."""

        self._require_started()
        if status not in {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }:
            raise ValueError("status must be a terminal RunStatus")
        completed_at = self.now()
        manifest = self._build_run_manifest(
            status,
            updated_at=completed_at,
            completed_at=completed_at,
        )
        self._save_run(manifest)
        return manifest

    def _build_run_manifest(
        self,
        status: RunStatus,
        *,
        updated_at: str,
        completed_at: str | None = None,
    ) -> RunManifest:
        started_at = self._require_started()
        return RunManifest(
            run_id=self.run_id,
            status=status,
            started_at=started_at,
            updated_at=updated_at,
            completed_at=completed_at,
            input_artifacts=self.input_artifacts,
            config=self.config,
            model_bindings=self.model_bindings,
            stages=tuple(self.stage_references),
        )

    def _save_run(self, manifest: RunManifest) -> None:
        try:
            self.run_store.save_run(manifest)
        except Exception as exc:
            raise EnginePersistenceError(
                "could not save a run manifest"
            ) from exc

    def _require_started(self) -> str:
        if self.started_at is None:
            raise EnginePersistenceError("run journal is not started")
        return self.started_at
