"""Sequential PipelineEngine orchestration over an Executor Port."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING

from video_preprocess.domain import (
    ArtifactRef,
    ModelExecution,
    RunManifest,
    RunStatus,
    StageAttemptRef,
    StageManifest,
    StageResult,
    StageSpec,
    StageStatus,
    StageTask,
)
from video_preprocess.executors.contracts import ExecutionHandle, Executor

from .cache import (
    CacheDecision,
    EffectiveModelResolver,
    ManifestCacheEvaluator,
    compute_stage_cache_key,
)
from .errors import (
    EngineInputError,
    EnginePersistenceError,
    StateTransitionError,
)
from .persistence import Clock, RunJournal, utc_now
from .planner import ExecutionPlan

if TYPE_CHECKING:
    from video_preprocess.storage.runs import RunStore


class StageLifecycle(str, Enum):
    """Engine-owned lifecycle for one planned Stage attempt."""

    PENDING = "pending"
    CACHED = "cached"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {
            self.SUCCEEDED,
            self.CACHED,
            self.SKIPPED,
            self.FAILED,
            self.CANCELLED,
        }


class StagePreviewStatus(str, Enum):
    """Read-only disposition for one planned Stage."""

    HIT = "hit"
    MISS = "miss"
    FORCED = "forced"
    BLOCKED = "blocked"


_STAGE_TRANSITIONS = {
    StageLifecycle.PENDING: {
        StageLifecycle.CACHED,
        StageLifecycle.QUEUED,
        StageLifecycle.FAILED,
        StageLifecycle.CANCELLED,
    },
    StageLifecycle.QUEUED: {
        StageLifecycle.RUNNING,
        StageLifecycle.FAILED,
        StageLifecycle.CANCELLED,
    },
    StageLifecycle.RUNNING: {
        StageLifecycle.SUCCEEDED,
        StageLifecycle.SKIPPED,
        StageLifecycle.FAILED,
        StageLifecycle.CANCELLED,
    },
}

_RUN_TRANSITIONS = {
    RunStatus.PENDING: {RunStatus.RUNNING},
    RunStatus.RUNNING: {
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    },
}


class StageStateMachine:
    """Validate and record one Stage attempt's lifecycle."""

    def __init__(self) -> None:
        self._history = [StageLifecycle.PENDING]

    @property
    def state(self) -> StageLifecycle:
        return self._history[-1]

    @property
    def history(self) -> tuple[StageLifecycle, ...]:
        return tuple(self._history)

    def transition(self, target: StageLifecycle) -> None:
        if not isinstance(target, StageLifecycle):
            raise TypeError("target must be a StageLifecycle")
        allowed = _STAGE_TRANSITIONS.get(self.state, set())
        if target not in allowed:
            raise StateTransitionError(
                f"invalid Stage transition: {self.state.value} → "
                f"{target.value}"
            )
        self._history.append(target)


class RunStateMachine:
    """Validate and record one pipeline run's lifecycle."""

    def __init__(self) -> None:
        self._history = [RunStatus.PENDING]

    @property
    def state(self) -> RunStatus:
        return self._history[-1]

    @property
    def history(self) -> tuple[RunStatus, ...]:
        return tuple(self._history)

    def transition(self, target: RunStatus) -> None:
        if not isinstance(target, RunStatus):
            raise TypeError("target must be a RunStatus")
        allowed = _RUN_TRANSITIONS.get(self.state, set())
        if target not in allowed:
            raise StateTransitionError(
                f"invalid run transition: {self.state.value} → "
                f"{target.value}"
            )
        self._history.append(target)


@dataclass(frozen=True, slots=True)
class StageExecutionRecord:
    """Final Engine view of one planned Stage attempt."""

    stage: str
    task: StageTask
    handle: ExecutionHandle | None
    result: StageResult
    transitions: tuple[StageLifecycle, ...]
    cache_decision: CacheDecision | None = None

    @property
    def state(self) -> StageLifecycle:
        return self.transitions[-1]

    @property
    def from_cache(self) -> bool:
        return (
            self.cache_decision is not None
            and self.cache_decision.hit
        )


@dataclass(frozen=True, slots=True)
class PipelineRunResult:
    """Terminal Engine result and optional persisted run manifest."""

    run_id: str
    status: RunStatus
    stages: tuple[StageExecutionRecord, ...]
    artifacts: dict[str, ArtifactRef]
    transitions: tuple[RunStatus, ...]
    manifest: RunManifest | None = None


@dataclass(frozen=True, slots=True)
class StagePreviewRecord:
    """Read-only cache view for one planned Stage."""

    stage: str
    status: StagePreviewStatus
    task: StageTask | None = None
    cache_decision: CacheDecision | None = None
    blocked_inputs: Sequence[str] = ()

    def __post_init__(self) -> None:
        blocked_inputs = tuple(self.blocked_inputs)
        object.__setattr__(self, "blocked_inputs", blocked_inputs)
        if self.status is StagePreviewStatus.BLOCKED:
            if self.task is not None or self.cache_decision is not None:
                raise ValueError("a blocked preview cannot contain a task")
            if not blocked_inputs:
                raise ValueError("a blocked preview requires missing inputs")
            return
        if self.task is None or self.cache_decision is None:
            raise ValueError("a cache preview requires a task and decision")
        if blocked_inputs:
            raise ValueError("a cache preview cannot contain blocked inputs")
        if self.status.value != self.cache_decision.status.value:
            raise ValueError("preview status must match cache decision")

    @property
    def will_execute(self) -> bool:
        return self.status in {
            StagePreviewStatus.MISS,
            StagePreviewStatus.FORCED,
        }


@dataclass(frozen=True, slots=True)
class PipelinePreviewResult:
    """Read-only Stage dispositions and artifacts proven reusable."""

    run_id: str
    stages: tuple[StagePreviewRecord, ...]
    artifacts: dict[str, ArtifactRef]


class PipelineEngine:
    """Build StageTask attempts and execute one topological plan in order."""

    def __init__(
        self,
        executor: Executor,
        *,
        run_store: RunStore | None = None,
        cache_evaluator: ManifestCacheEvaluator | None = None,
        model_resolver: EffectiveModelResolver | None = None,
        clock: Clock = utc_now,
    ) -> None:
        for method_name in ("submit", "status", "result", "cancel"):
            if not callable(getattr(executor, method_name, None)):
                raise TypeError("executor must implement the Executor Port")
        self.executor = executor
        if run_store is not None:
            for method_name in ("save_run", "load_stage", "save_stage"):
                if not callable(getattr(run_store, method_name, None)):
                    raise TypeError(
                        "run_store must implement the RunStore Port"
                    )
        if cache_evaluator is not None and not callable(
            getattr(cache_evaluator, "evaluate", None)
        ):
            raise TypeError("cache_evaluator must implement evaluate")
        if cache_evaluator is not None and run_store is None:
            raise ValueError("cache_evaluator requires a run_store")
        if model_resolver is not None and not callable(
            getattr(model_resolver, "resolve", None)
        ):
            raise TypeError("model_resolver must implement resolve")
        if model_resolver is not None and cache_evaluator is None:
            raise ValueError("model_resolver requires a cache_evaluator")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.run_store = run_store
        self.cache_evaluator = cache_evaluator
        self.model_resolver = model_resolver
        self.clock = clock

    async def preview(
        self,
        plan: ExecutionPlan,
        *,
        run_id: str,
        trace_id: str,
        artifacts: Mapping[str, ArtifactRef],
        stage_configs: Mapping[str, Mapping[str, object]] | None = None,
        model_bindings: Mapping[str, Mapping[str, str]] | None = None,
        attempts: Mapping[str, int] | None = None,
        force_stages: Collection[str] | None = None,
    ) -> PipelinePreviewResult:
        """Evaluate cache reuse without submitting or persisting work."""

        if self.cache_evaluator is None or self.run_store is None:
            raise EngineInputError(
                "cache preview requires a cache evaluator and run store"
            )
        if not isinstance(plan, ExecutionPlan):
            raise TypeError("plan must be an ExecutionPlan")
        run_id = self._required_string(run_id, "run_id")
        trace_id = self._required_string(trace_id, "trace_id")
        available = self._normalize_artifacts(artifacts)
        configs = self._normalize_nested_mapping(
            stage_configs,
            "stage_configs",
        )
        bindings = self._normalize_nested_mapping(
            model_bindings,
            "model_bindings",
        )
        normalized_attempts = {} if attempts is None else dict(attempts)
        forced = self._normalize_force_stages(force_stages)
        self._validate_plan_options(
            plan,
            available,
            configs,
            bindings,
            normalized_attempts,
            forced,
            require_boundary=False,
        )
        self._validate_task_payloads(
            plan,
            run_id=run_id,
            trace_id=trace_id,
            configs=configs,
            bindings=bindings,
            attempts=normalized_attempts,
        )

        records = []
        for spec in plan.stages:
            missing_inputs = tuple(
                input_name
                for input_name in spec.required_inputs
                if input_name not in available
            )
            if missing_inputs:
                records.append(
                    StagePreviewRecord(
                        stage=spec.name,
                        status=StagePreviewStatus.BLOCKED,
                        blocked_inputs=missing_inputs,
                    )
                )
                continue
            task = self._build_task(
                spec,
                run_id=run_id,
                trace_id=trace_id,
                available=available,
                config=configs.get(spec.name, {}),
                model_bindings=bindings.get(spec.name, {}),
                attempt=normalized_attempts.get(spec.name, 1),
            )
            decision = await self._evaluate_cache_candidate(
                task,
                candidate=(
                    None
                    if spec.name in forced
                    else self._load_cache_candidate(task)
                ),
                force=spec.name in forced,
            )
            record = StagePreviewRecord(
                stage=spec.name,
                status=StagePreviewStatus(decision.status.value),
                task=task,
                cache_decision=decision,
            )
            records.append(record)
            if decision.hit:
                available.update(decision.outputs)

        return PipelinePreviewResult(
            run_id=run_id,
            stages=tuple(records),
            artifacts=dict(available),
        )

    async def run(
        self,
        plan: ExecutionPlan,
        *,
        run_id: str,
        trace_id: str,
        artifacts: Mapping[str, ArtifactRef],
        stage_configs: Mapping[str, Mapping[str, object]] | None = None,
        model_bindings: Mapping[str, Mapping[str, str]] | None = None,
        attempts: Mapping[str, int] | None = None,
        force_stages: Collection[str] | None = None,
    ) -> PipelineRunResult:
        if not isinstance(plan, ExecutionPlan):
            raise TypeError("plan must be an ExecutionPlan")
        run_id = self._required_string(run_id, "run_id")
        trace_id = self._required_string(trace_id, "trace_id")
        available = self._normalize_artifacts(artifacts)
        configs = self._normalize_nested_mapping(
            stage_configs,
            "stage_configs",
        )
        bindings = self._normalize_nested_mapping(
            model_bindings,
            "model_bindings",
        )
        normalized_attempts = {} if attempts is None else dict(attempts)
        forced = self._normalize_force_stages(force_stages)
        if forced and self.cache_evaluator is None:
            raise EngineInputError(
                "force_stages requires a configured cache evaluator"
            )
        self._validate_plan_options(
            plan,
            available,
            configs,
            bindings,
            normalized_attempts,
            forced,
        )
        self._validate_task_payloads(
            plan,
            run_id=run_id,
            trace_id=trace_id,
            configs=configs,
            bindings=bindings,
            attempts=normalized_attempts,
        )

        run_state = RunStateMachine()
        run_state.transition(RunStatus.RUNNING)
        journal = self._create_journal(
            run_id=run_id,
            artifacts=available,
            configs=configs,
            bindings=bindings,
        )
        if journal is not None:
            journal.start()
        records = []
        for spec in plan.stages:
            stage_state = StageStateMachine()
            task = self._build_task(
                spec,
                run_id=run_id,
                trace_id=trace_id,
                available=available,
                config=configs.get(spec.name, {}),
                model_bindings=bindings.get(spec.name, {}),
                attempt=normalized_attempts.get(spec.name, 1),
            )
            stage_started_at = journal.now() if journal is not None else None
            missing_inputs = tuple(
                input_name
                for input_name in spec.required_inputs
                if input_name not in available
            )
            if missing_inputs:
                result = self._failed_result(
                    task,
                    "MISSING_REQUIRED_INPUT",
                    "required Stage input is unavailable: "
                    + ", ".join(missing_inputs),
                )
                stage_state.transition(StageLifecycle.FAILED)
                self._persist_stage(
                    journal,
                    task,
                    result,
                    started_at=stage_started_at,
                    cache_key=compute_stage_cache_key(task),
                )
                records.append(
                    StageExecutionRecord(
                        stage=spec.name,
                        task=task,
                        handle=None,
                        result=result,
                        transitions=stage_state.history,
                    )
                )
                run_state.transition(RunStatus.FAILED)
                break

            cache_decision = await self._evaluate_cache(
                task,
                journal,
                force=spec.name in forced,
            )
            if cache_decision is not None and cache_decision.hit:
                cached_manifest = cache_decision.manifest
                if cached_manifest is None:
                    raise RuntimeError("cache hit is missing its manifest")
                result = replace(
                    cached_manifest.result,
                    run_id=task.run_id,
                    stage_run_id=task.stage_run_id,
                    attempt=task.attempt,
                )
                result = self._validate_result_outputs(spec, task, result)
                if result.status is StageStatus.SUCCEEDED:
                    stage_state.transition(StageLifecycle.CACHED)
                else:
                    stage_state.transition(StageLifecycle.FAILED)
                self._persist_stage(
                    journal,
                    task,
                    result,
                    started_at=stage_started_at,
                    cache_key=cache_decision.cache_key,
                )
                records.append(
                    StageExecutionRecord(
                        stage=spec.name,
                        task=task,
                        handle=None,
                        result=result,
                        transitions=stage_state.history,
                        cache_decision=cache_decision,
                    )
                )
                if result.status is StageStatus.SUCCEEDED:
                    available.update(result.outputs)
                    continue
                run_state.transition(RunStatus.FAILED)
                break

            try:
                handle = await self.executor.submit(task)
            except Exception as exc:
                result = self._failed_result(
                    task,
                    "EXECUTOR_SUBMIT_FAILED",
                    "Executor could not submit the StageTask",
                    warning=f"error_type={type(exc).__name__}",
                )
                stage_state.transition(StageLifecycle.FAILED)
                self._persist_stage(
                    journal,
                    task,
                    result,
                    started_at=stage_started_at,
                    cache_key=self._cache_key(task, cache_decision),
                )
                records.append(
                    StageExecutionRecord(
                        stage=spec.name,
                        task=task,
                        handle=None,
                        result=result,
                        transitions=stage_state.history,
                        cache_decision=cache_decision,
                    )
                )
                run_state.transition(RunStatus.FAILED)
                break

            stage_state.transition(StageLifecycle.QUEUED)
            stage_state.transition(StageLifecycle.RUNNING)
            try:
                result = await self.executor.result(handle)
            except Exception as exc:
                result = self._failed_result(
                    task,
                    "EXECUTOR_RESULT_FAILED",
                    "Executor could not return the StageResult",
                    warning=f"error_type={type(exc).__name__}",
                )
            result = self._validate_result_outputs(spec, task, result)
            terminal_state = self._lifecycle_from_result(result.status)
            stage_state.transition(terminal_state)
            self._persist_stage(
                journal,
                task,
                result,
                started_at=stage_started_at,
                cache_key=self._cache_key(task, cache_decision),
            )
            records.append(
                StageExecutionRecord(
                    stage=spec.name,
                    task=task,
                    handle=handle,
                    result=result,
                    transitions=stage_state.history,
                    cache_decision=cache_decision,
                )
            )
            if result.status in {StageStatus.SUCCEEDED, StageStatus.SKIPPED}:
                available.update(result.outputs)
                continue
            if result.status is StageStatus.CANCELLED:
                run_state.transition(RunStatus.CANCELLED)
            else:
                run_state.transition(RunStatus.FAILED)
            break
        else:
            run_state.transition(RunStatus.SUCCEEDED)

        run_manifest = (
            None
            if journal is None
            else journal.finish(run_state.state)
        )

        return PipelineRunResult(
            run_id=run_id,
            status=run_state.state,
            stages=tuple(records),
            artifacts=dict(available),
            transitions=run_state.history,
            manifest=run_manifest,
        )

    def _create_journal(
        self,
        *,
        run_id: str,
        artifacts: Mapping[str, ArtifactRef],
        configs: Mapping[str, Mapping[str, object]],
        bindings: Mapping[str, Mapping[str, object]],
    ) -> RunJournal | None:
        if self.run_store is None:
            return None
        return RunJournal(
            self.run_store,
            run_id=run_id,
            input_artifacts=artifacts,
            stage_configs=configs,
            model_bindings=bindings,
            clock=self.clock,
        )

    async def _evaluate_cache(
        self,
        task: StageTask,
        journal: RunJournal | None,
        *,
        force: bool,
    ) -> CacheDecision | None:
        if self.cache_evaluator is None:
            return None
        if journal is None:
            raise RuntimeError("cache evaluation requires a run journal")
        candidate = None if force else journal.load_candidate(task)
        return await self._evaluate_cache_candidate(
            task,
            candidate=candidate,
            force=force,
        )

    async def _evaluate_cache_candidate(
        self,
        task: StageTask,
        *,
        candidate: StageManifest | None,
        force: bool,
    ) -> CacheDecision:
        if self.cache_evaluator is None:
            raise RuntimeError("cache evaluator is unavailable")
        expected_models: Sequence[ModelExecution] | None
        if candidate is None or not task.model_bindings:
            expected_models = () if not task.model_bindings else None
        elif self.model_resolver is None:
            expected_models = None
        else:
            try:
                expected_models = await self.model_resolver.resolve(task)
            except Exception:
                expected_models = None
        return self.cache_evaluator.evaluate(
            task,
            candidate,
            expected_models=expected_models,
            force=force,
        )

    def _load_cache_candidate(self, task: StageTask) -> StageManifest | None:
        if self.run_store is None:
            raise RuntimeError("cache candidate loading requires a run store")
        try:
            return self.run_store.load_stage(
                task.run_id,
                StageAttemptRef(task.stage_run_id, task.attempt),
            )
        except Exception as exc:
            raise EnginePersistenceError(
                "could not load a Stage cache candidate"
            ) from exc

    @staticmethod
    def _persist_stage(
        journal: RunJournal | None,
        task: StageTask,
        result: StageResult,
        *,
        started_at: str | None,
        cache_key: str,
    ) -> None:
        if journal is None:
            return
        if started_at is None:
            raise RuntimeError("persisted Stage is missing its start time")
        journal.record_stage(
            task,
            result,
            started_at=started_at,
            completed_at=journal.now(),
            cache_key=cache_key,
        )

    @staticmethod
    def _cache_key(
        task: StageTask,
        decision: CacheDecision | None,
    ) -> str:
        if decision is not None:
            return decision.cache_key
        return compute_stage_cache_key(task)

    @classmethod
    def _build_task(
        cls,
        spec: StageSpec,
        *,
        run_id: str,
        trace_id: str,
        available: Mapping[str, ArtifactRef],
        config: Mapping[str, object],
        model_bindings: Mapping[str, str],
        attempt: int,
    ) -> StageTask:
        stage_run_digest = hashlib.sha256(
            f"{run_id}\0{spec.name}".encode("utf-8")
        ).hexdigest()[:24]
        stage_run_id = f"stage_{stage_run_digest}"
        inputs = {
            input_name: available[input_name]
            for input_name in spec.required_inputs
            if input_name in available
        }
        preliminary = StageTask(
            run_id=run_id,
            stage_run_id=stage_run_id,
            attempt=attempt,
            stage=spec.name,
            stage_version=spec.stage_version,
            inputs=inputs,
            config=config,
            model_bindings=model_bindings,
            idempotency_key="pending",
            trace_id=trace_id,
        )
        payload = preliminary.to_dict()
        payload.pop("idempotency_key")
        payload.pop("trace_id")
        fingerprint = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return replace(
            preliminary,
            idempotency_key=f"stage_{fingerprint}",
        )

    @classmethod
    def _validate_result_outputs(
        cls,
        spec: StageSpec,
        task: StageTask,
        result: object,
    ) -> StageResult:
        if not isinstance(result, StageResult):
            return cls._failed_result(
                task,
                "EXECUTOR_INVALID_RESULT",
                "Executor returned a non-StageResult value",
            )
        if (
            result.run_id != task.run_id
            or result.stage_run_id != task.stage_run_id
            or result.attempt != task.attempt
        ):
            return cls._failed_result(
                task,
                "EXECUTOR_RESULT_ID_MISMATCH",
                "StageResult identity does not match StageTask",
            )
        unexpected = sorted(set(result.outputs) - set(spec.outputs))
        if unexpected:
            return cls._failed_result(
                task,
                "UNDECLARED_STAGE_OUTPUT",
                "StageResult contains undeclared output: "
                + ", ".join(unexpected),
            )
        return result

    @staticmethod
    def _validate_plan_options(
        plan: ExecutionPlan,
        artifacts: Mapping[str, ArtifactRef],
        configs: Mapping[str, Mapping[str, object]],
        bindings: Mapping[str, Mapping[str, object]],
        attempts: Mapping[str, object],
        forced: Collection[str],
        *,
        require_boundary: bool = True,
    ) -> None:
        stage_names = set(plan.stage_names)
        for field_name, mapping in (
            ("stage_configs", configs),
            ("model_bindings", bindings),
            ("attempts", attempts),
        ):
            unknown = sorted(set(mapping) - stage_names)
            if unknown:
                raise EngineInputError(
                    f"{field_name} contains unplanned stage: {unknown[0]}"
                )
        unknown_forced = sorted(set(forced) - stage_names)
        if unknown_forced:
            raise EngineInputError(
                f"force_stages contains unplanned stage: {unknown_forced[0]}"
            )
        missing_boundary = sorted(set(plan.boundary_inputs) - set(artifacts))
        if require_boundary and missing_boundary:
            raise EngineInputError(
                "missing plan boundary input: " + ", ".join(missing_boundary)
            )
        for spec in plan.stages:
            stage_bindings = bindings.get(spec.name, {})
            expected_slots = set(spec.model_slots)
            actual_slots = set(stage_bindings)
            if actual_slots != expected_slots:
                missing = sorted(expected_slots - actual_slots)
                extra = sorted(actual_slots - expected_slots)
                detail = []
                if missing:
                    detail.append("missing " + ", ".join(missing))
                if extra:
                    detail.append("unexpected " + ", ".join(extra))
                raise EngineInputError(
                    f"model_bindings.{spec.name}: " + "; ".join(detail)
                )
            attempt = attempts.get(spec.name, 1)
            if (
                isinstance(attempt, bool)
                or not isinstance(attempt, int)
                or attempt < 1
            ):
                raise EngineInputError(
                    f"attempts.{spec.name} must be a positive integer"
                )

    @staticmethod
    def _validate_task_payloads(
        plan: ExecutionPlan,
        *,
        run_id: str,
        trace_id: str,
        configs: Mapping[str, Mapping[str, object]],
        bindings: Mapping[str, Mapping[str, object]],
        attempts: Mapping[str, int],
    ) -> None:
        for spec in plan.stages:
            try:
                StageTask(
                    run_id=run_id,
                    stage_run_id="stage_payload_validation",
                    attempt=attempts.get(spec.name, 1),
                    stage=spec.name,
                    stage_version=spec.stage_version,
                    inputs={},
                    config=configs.get(spec.name, {}),
                    model_bindings=bindings.get(spec.name, {}),
                    idempotency_key="payload_validation",
                    trace_id=trace_id,
                )
            except (TypeError, ValueError) as exc:
                raise EngineInputError(
                    f"invalid StageTask payload for {spec.name}: {exc}"
                ) from exc

    @staticmethod
    def _normalize_artifacts(
        artifacts: Mapping[str, ArtifactRef],
    ) -> dict[str, ArtifactRef]:
        if not isinstance(artifacts, Mapping):
            raise TypeError("artifacts must be a mapping")
        normalized = {}
        for key, artifact in artifacts.items():
            if not isinstance(key, str) or not key.strip():
                raise EngineInputError(
                    "artifact keys must be non-empty strings"
                )
            if not isinstance(artifact, ArtifactRef):
                raise EngineInputError(
                    f"artifacts.{key} must be an ArtifactRef"
                )
            normalized[key] = artifact
        return normalized

    @staticmethod
    def _normalize_nested_mapping(
        value: Mapping[str, Mapping[str, object]] | None,
        field_name: str,
    ) -> dict[str, dict[str, object]]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise TypeError(f"{field_name} must be a mapping")
        normalized = {}
        for stage_name, stage_value in value.items():
            if not isinstance(stage_name, str) or not stage_name.strip():
                raise EngineInputError(
                    f"{field_name} stage names must be non-empty strings"
                )
            if not isinstance(stage_value, Mapping):
                raise EngineInputError(
                    f"{field_name}.{stage_name} must be a mapping"
                )
            normalized[stage_name] = dict(stage_value)
        return normalized

    @staticmethod
    def _normalize_force_stages(
        value: Collection[str] | None,
    ) -> frozenset[str]:
        if value is None:
            return frozenset()
        if isinstance(value, (str, bytes)) or not isinstance(
            value,
            Collection,
        ):
            raise TypeError("force_stages must be a collection of Stage names")
        normalized = set()
        for stage_name in value:
            if not isinstance(stage_name, str) or not stage_name.strip():
                raise EngineInputError(
                    "force_stages must contain non-empty Stage names"
                )
            normalized.add(stage_name.strip())
        return frozenset(normalized)

    @staticmethod
    def _required_string(value: object, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise EngineInputError(
                f"{field_name} must be a non-empty string"
            )
        return value.strip()

    @staticmethod
    def _lifecycle_from_result(status: StageStatus) -> StageLifecycle:
        return {
            StageStatus.SUCCEEDED: StageLifecycle.SUCCEEDED,
            StageStatus.SKIPPED: StageLifecycle.SKIPPED,
            StageStatus.FAILED: StageLifecycle.FAILED,
            StageStatus.CANCELLED: StageLifecycle.CANCELLED,
        }[status]

    @staticmethod
    def _failed_result(
        task: StageTask,
        reason_code: str,
        reason: str,
        *,
        warning: str | None = None,
    ) -> StageResult:
        return StageResult(
            run_id=task.run_id,
            stage_run_id=task.stage_run_id,
            attempt=task.attempt,
            status=StageStatus.FAILED,
            reason_code=reason_code,
            reason=reason,
            warnings=() if warning is None else (warning,),
        )
