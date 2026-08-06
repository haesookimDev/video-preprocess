"""Single-process sequential Executor for injected StageTask runners."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
from dataclasses import dataclass

from video_preprocess.domain import StageResult, StageStatus, StageTask

from .bindings import StageBindingRegistry, StageRunner
from .contracts import ExecutionHandle, ExecutionState, ExecutionStatus
from .errors import (
    DuplicateSubmissionError,
    IdempotencyConflictError,
    UnknownExecutionError,
)


@dataclass(slots=True)
class _Job:
    task: StageTask
    handle: ExecutionHandle
    runner: StageRunner
    completion: asyncio.Future[StageResult]
    state: ExecutionState = ExecutionState.QUEUED
    cancel_requested: bool = False


class LocalExecutor:
    """Execute one bound StageTask at a time in the current process."""

    def __init__(self, bindings: StageBindingRegistry) -> None:
        if not isinstance(bindings, StageBindingRegistry):
            raise TypeError("bindings must be a StageBindingRegistry")
        self.bindings = bindings
        self._serial_lock = asyncio.Lock()
        self._jobs: dict[str, _Job] = {}
        self._idempotency: dict[str, str] = {}
        self._attempts: dict[tuple[str, int], str] = {}
        self._background_tasks: set[asyncio.Task[None]] = set()

    async def submit(self, task: StageTask) -> ExecutionHandle:
        if not isinstance(task, StageTask):
            raise TypeError("task must be a StageTask")
        runner = self.bindings.get(task.stage)
        existing_id = self._idempotency.get(task.idempotency_key)
        if existing_id is not None:
            existing = self._jobs[existing_id]
            if existing.task != task:
                raise IdempotencyConflictError(
                    "idempotency key was reused for a different StageTask"
                )
            return existing.handle

        attempt_key = (task.stage_run_id, task.attempt)
        existing_attempt_id = self._attempts.get(attempt_key)
        if existing_attempt_id is not None:
            raise DuplicateSubmissionError(
                "stage_run_id and attempt were already submitted"
            )

        execution_id = self._execution_id(task)
        handle = ExecutionHandle(
            execution_id=execution_id,
            stage_run_id=task.stage_run_id,
            attempt=task.attempt,
        )
        completion = asyncio.get_running_loop().create_future()
        job = _Job(
            task=task,
            handle=handle,
            runner=runner,
            completion=completion,
        )
        self._jobs[execution_id] = job
        self._idempotency[task.idempotency_key] = execution_id
        self._attempts[attempt_key] = execution_id
        background = asyncio.create_task(self._execute(job))
        self._background_tasks.add(background)
        background.add_done_callback(self._background_tasks.discard)
        return handle

    async def status(self, handle: ExecutionHandle) -> ExecutionStatus:
        job = self._job(handle)
        return ExecutionStatus(
            handle=job.handle,
            state=job.state,
            cancel_requested=job.cancel_requested,
        )

    async def result(self, handle: ExecutionHandle) -> StageResult:
        job = self._job(handle)
        return await asyncio.shield(job.completion)

    async def cancel(self, handle: ExecutionHandle) -> None:
        job = self._job(handle)
        if job.state.terminal:
            return
        job.cancel_requested = True
        if job.state is ExecutionState.QUEUED:
            self._finish(job, self._cancelled_result(job.task))

    async def _execute(self, job: _Job) -> None:
        try:
            async with self._serial_lock:
                if job.completion.done():
                    return
                if job.cancel_requested:
                    self._finish(job, self._cancelled_result(job.task))
                    return
                job.state = ExecutionState.RUNNING
                try:
                    result = await self._call_runner(job.runner, job.task)
                except Exception as exc:
                    result = self._failed_result(
                        job.task,
                        "STAGE_EXCEPTION",
                        "stage runner raised an exception",
                        warning=f"error_type={type(exc).__name__}",
                    )
                if job.cancel_requested:
                    result = self._cancelled_result(job.task)
                else:
                    result = self._normalize_result(job.task, result)
                self._finish(job, result)
        except asyncio.CancelledError:
            if not job.completion.done():
                self._finish(job, self._cancelled_result(job.task))
            raise

    @staticmethod
    async def _call_runner(
        runner: StageRunner,
        task: StageTask,
    ) -> object:
        call = runner
        if not inspect.iscoroutinefunction(call):
            call_method = getattr(call, "__call__", None)
            if inspect.iscoroutinefunction(call_method):
                return await call(task)
            result = await asyncio.to_thread(call, task)
            if inspect.isawaitable(result):
                return await result
            return result
        return await call(task)

    @classmethod
    def _normalize_result(
        cls,
        task: StageTask,
        result: object,
    ) -> StageResult:
        if not isinstance(result, StageResult):
            return cls._failed_result(
                task,
                "EXECUTOR_INVALID_RESULT",
                "stage runner returned a non-StageResult value",
                warning=f"result_type={type(result).__name__}",
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
        return result

    def _finish(self, job: _Job, result: StageResult) -> None:
        job.state = self._state_from_result(result.status)
        if not job.completion.done():
            job.completion.set_result(result)

    def _job(self, handle: ExecutionHandle) -> _Job:
        if not isinstance(handle, ExecutionHandle):
            raise TypeError("handle must be an ExecutionHandle")
        job = self._jobs.get(handle.execution_id)
        if job is None or job.handle != handle:
            raise UnknownExecutionError(
                f"unknown execution handle: {handle.execution_id}"
            )
        return job

    @staticmethod
    def _state_from_result(status: StageStatus) -> ExecutionState:
        return {
            StageStatus.SUCCEEDED: ExecutionState.SUCCEEDED,
            StageStatus.SKIPPED: ExecutionState.SKIPPED,
            StageStatus.FAILED: ExecutionState.FAILED,
            StageStatus.CANCELLED: ExecutionState.CANCELLED,
        }[status]

    @staticmethod
    def _execution_id(task: StageTask) -> str:
        digest = hashlib.sha256(
            (
                f"{task.run_id}\0{task.stage_run_id}\0{task.attempt}\0"
                f"{task.idempotency_key}"
            ).encode("utf-8")
        ).hexdigest()[:24]
        return f"exec_{digest}"

    @staticmethod
    def _cancelled_result(task: StageTask) -> StageResult:
        return StageResult(
            run_id=task.run_id,
            stage_run_id=task.stage_run_id,
            attempt=task.attempt,
            status=StageStatus.CANCELLED,
            reason_code="EXECUTOR_CANCELLED",
            reason="execution was cancelled",
        )

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
