"""Sequential PipelineEngine orchestration over an Executor Port."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import Enum

from video_preprocess.domain import (
    ArtifactRef,
    RunStatus,
    StageResult,
    StageSpec,
    StageStatus,
    StageTask,
)
from video_preprocess.executors.contracts import ExecutionHandle, Executor

from .errors import EngineInputError, StateTransitionError
from .planner import ExecutionPlan


class StageLifecycle(str, Enum):
    """Engine-owned lifecycle for one planned Stage attempt."""

    PENDING = "pending"
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
            self.SKIPPED,
            self.FAILED,
            self.CANCELLED,
        }


_STAGE_TRANSITIONS = {
    StageLifecycle.PENDING: {
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

    @property
    def state(self) -> StageLifecycle:
        return self.transitions[-1]


@dataclass(frozen=True, slots=True)
class PipelineRunResult:
    """Terminal in-memory run result before manifest persistence."""

    run_id: str
    status: RunStatus
    stages: tuple[StageExecutionRecord, ...]
    artifacts: dict[str, ArtifactRef]
    transitions: tuple[RunStatus, ...]


class PipelineEngine:
    """Build StageTask attempts and execute one topological plan in order."""

    def __init__(self, executor: Executor) -> None:
        for method_name in ("submit", "status", "result", "cancel"):
            if not callable(getattr(executor, method_name, None)):
                raise TypeError("executor must implement the Executor Port")
        self.executor = executor

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
        self._validate_plan_options(
            plan,
            available,
            configs,
            bindings,
            normalized_attempts,
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
            records.append(
                StageExecutionRecord(
                    stage=spec.name,
                    task=task,
                    handle=handle,
                    result=result,
                    transitions=stage_state.history,
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

        return PipelineRunResult(
            run_id=run_id,
            status=run_state.state,
            stages=tuple(records),
            artifacts=dict(available),
            transitions=run_state.history,
        )

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
        missing_boundary = sorted(set(plan.boundary_inputs) - set(artifacts))
        if missing_boundary:
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
