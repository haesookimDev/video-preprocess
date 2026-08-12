"""Contract tests for the single-process sequential LocalExecutor."""

import asyncio
import threading
from dataclasses import replace

import pytest

from video_preprocess.domain import StageResult, StageStatus, StageTask
from video_preprocess.executors import (
    CancellationToken,
    DuplicateSubmissionError,
    ExecutionControl,
    ExecutionHandle,
    ExecutionState,
    IdempotencyConflictError,
    LocalExecutor,
    StageBindingRegistry,
    UnknownExecutionError,
    UnknownStageBindingError,
)


def make_task(
    *,
    stage_run_id: str = "stage_123",
    attempt: int = 1,
    idempotency_key: str = "idem_123",
    config=None,
) -> StageTask:
    return StageTask(
        run_id="run_123",
        stage_run_id=stage_run_id,
        attempt=attempt,
        stage="test_stage",
        stage_version="1.0.0",
        inputs={},
        config={} if config is None else config,
        model_bindings={},
        idempotency_key=idempotency_key,
        trace_id="trace_123",
    )


def make_result(
    task: StageTask,
    status: StageStatus = StageStatus.SUCCEEDED,
) -> StageResult:
    return StageResult(
        run_id=task.run_id,
        stage_run_id=task.stage_run_id,
        attempt=task.attempt,
        status=status,
    )


def test_submit_returns_handle_and_successful_result() -> None:
    async def scenario():
        seen = []

        async def runner(task):
            seen.append(task)
            return make_result(task)

        executor = LocalExecutor(
            StageBindingRegistry([("test_stage", runner)])
        )
        task = make_task()

        handle = await executor.submit(task)
        queued = await executor.status(handle)
        result = await executor.result(handle)
        completed = await executor.status(handle)

        assert handle.stage_run_id == task.stage_run_id
        assert queued.state is ExecutionState.QUEUED
        assert result == make_result(task)
        assert completed.state is ExecutionState.SUCCEEDED
        assert seen == [task]

    asyncio.run(scenario())


def test_executor_runs_concurrent_submissions_sequentially() -> None:
    async def scenario():
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        order = []

        async def runner(task):
            order.append(f"start:{task.stage_run_id}")
            if task.stage_run_id == "stage_1":
                first_started.set()
                await release_first.wait()
            order.append(f"end:{task.stage_run_id}")
            return make_result(task)

        executor = LocalExecutor(
            StageBindingRegistry([("test_stage", runner)])
        )
        first = await executor.submit(
            make_task(stage_run_id="stage_1", idempotency_key="idem_1")
        )
        second = await executor.submit(
            make_task(stage_run_id="stage_2", idempotency_key="idem_2")
        )
        await first_started.wait()

        assert (await executor.status(first)).state is ExecutionState.RUNNING
        assert (await executor.status(second)).state is ExecutionState.QUEUED

        release_first.set()
        await asyncio.gather(
            executor.result(first),
            executor.result(second),
        )

        assert order == [
            "start:stage_1",
            "end:stage_1",
            "start:stage_2",
            "end:stage_2",
        ]

    asyncio.run(scenario())


def test_sync_runner_executes_off_event_loop_thread() -> None:
    async def scenario():
        loop_thread = threading.get_ident()
        runner_threads = []

        def runner(task):
            runner_threads.append(threading.get_ident())
            return make_result(task)

        executor = LocalExecutor(
            StageBindingRegistry([("test_stage", runner)])
        )
        handle = await executor.submit(make_task())
        result = await executor.result(handle)

        assert result.status is StageStatus.SUCCEEDED
        assert runner_threads[0] != loop_thread

    asyncio.run(scenario())


def test_executor_passes_execution_control_to_aware_runner() -> None:
    async def scenario():
        seen = []

        async def runner(task, control):
            seen.append(control)
            return make_result(task)

        executor = LocalExecutor(
            StageBindingRegistry([("test_stage", runner)])
        )
        control = ExecutionControl(timeout_sec=12.5)
        result = await executor.result(
            await executor.submit(make_task(), control=control)
        )

        assert result.status is StageStatus.SUCCEEDED
        assert seen == [control]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("stage_status", "execution_state"),
    [
        (StageStatus.SKIPPED, ExecutionState.SKIPPED),
        (StageStatus.FAILED, ExecutionState.FAILED),
        (StageStatus.CANCELLED, ExecutionState.CANCELLED),
    ],
)
def test_executor_maps_terminal_stage_status(
    stage_status: StageStatus,
    execution_state: ExecutionState,
) -> None:
    async def scenario():
        executor = LocalExecutor(
            StageBindingRegistry(
                [("test_stage", lambda task: make_result(task, stage_status))]
            )
        )
        handle = await executor.submit(make_task())

        assert (await executor.result(handle)).status is stage_status
        assert (await executor.status(handle)).state is execution_state

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("runner", "reason_code"),
    [
        (
            lambda task: (_ for _ in ()).throw(RuntimeError("secret")),
            "STAGE_EXCEPTION",
        ),
        (lambda task: {"not": "a result"}, "EXECUTOR_INVALID_RESULT"),
        (
            lambda task: StageResult(
                run_id=task.run_id,
                stage_run_id="wrong_stage_run",
                attempt=task.attempt,
                status=StageStatus.SUCCEEDED,
            ),
            "EXECUTOR_RESULT_ID_MISMATCH",
        ),
    ],
)
def test_executor_normalizes_runner_failures(runner, reason_code) -> None:
    async def scenario():
        executor = LocalExecutor(
            StageBindingRegistry([("test_stage", runner)])
        )
        handle = await executor.submit(make_task())

        result = await executor.result(handle)

        assert result.status is StageStatus.FAILED
        assert result.reason_code == reason_code
        assert (await executor.status(handle)).state is ExecutionState.FAILED
        assert "secret" not in (result.reason or "")

    asyncio.run(scenario())


def test_sync_callable_returning_awaitable_is_supported() -> None:
    async def scenario():
        async def complete(task):
            return make_result(task)

        def runner(task):
            return complete(task)

        executor = LocalExecutor(
            StageBindingRegistry([("test_stage", runner)])
        )
        handle = await executor.submit(make_task())

        assert (await executor.result(handle)).status is StageStatus.SUCCEEDED

    asyncio.run(scenario())


def test_idempotent_resubmission_returns_same_handle() -> None:
    async def scenario():
        calls = 0

        async def runner(task):
            nonlocal calls
            calls += 1
            return make_result(task)

        executor = LocalExecutor(
            StageBindingRegistry([("test_stage", runner)])
        )
        task = make_task()

        first = await executor.submit(task)
        second = await executor.submit(task)
        await executor.result(first)

        assert first == second
        assert calls == 1

    asyncio.run(scenario())


def test_executor_rejects_idempotency_and_attempt_conflicts() -> None:
    async def scenario():
        executor = LocalExecutor(
            StageBindingRegistry(
                [("test_stage", lambda task: make_result(task))]
            )
        )
        original = make_task()
        await executor.submit(original)

        with pytest.raises(IdempotencyConflictError):
            await executor.submit(
                replace(original, config={"changed": True})
            )
        with pytest.raises(DuplicateSubmissionError):
            await executor.submit(
                replace(original, idempotency_key="different_key")
            )

        await executor.result(
            await executor.submit(original)
        )

    asyncio.run(scenario())


def test_cancel_queued_task_finishes_without_running_it() -> None:
    async def scenario():
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        seen = []

        async def runner(task):
            seen.append(task.stage_run_id)
            if task.stage_run_id == "stage_1":
                first_started.set()
                await release_first.wait()
            return make_result(task)

        executor = LocalExecutor(
            StageBindingRegistry([("test_stage", runner)])
        )
        first = await executor.submit(
            make_task(stage_run_id="stage_1", idempotency_key="idem_1")
        )
        second = await executor.submit(
            make_task(stage_run_id="stage_2", idempotency_key="idem_2")
        )
        await first_started.wait()

        await executor.cancel(second)
        cancelled = await executor.result(second)
        release_first.set()
        await executor.result(first)

        assert cancelled.status is StageStatus.CANCELLED
        assert cancelled.reason_code == "EXECUTOR_CANCELLED"
        assert seen == ["stage_1"]

    asyncio.run(scenario())


def test_cancel_running_task_discards_runner_result() -> None:
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        async def runner(task):
            started.set()
            await release.wait()
            return make_result(task)

        executor = LocalExecutor(
            StageBindingRegistry([("test_stage", runner)])
        )
        handle = await executor.submit(make_task())
        await started.wait()

        await executor.cancel(handle)
        status = await executor.status(handle)
        assert status.state is ExecutionState.RUNNING
        assert status.cancel_requested

        release.set()
        result = await executor.result(handle)

        assert result.status is StageStatus.CANCELLED
        assert (await executor.status(handle)).state is ExecutionState.CANCELLED

    asyncio.run(scenario())


def test_cancellation_token_skips_queued_and_notifies_running_runner() -> None:
    async def scenario():
        token = CancellationToken()
        token.cancel()
        queued_calls = 0

        async def queued_runner(task, control):
            nonlocal queued_calls
            queued_calls += 1
            return make_result(task)

        queued_executor = LocalExecutor(
            StageBindingRegistry([("test_stage", queued_runner)])
        )
        queued = await queued_executor.submit(
            make_task(),
            control=ExecutionControl(cancellation=token),
        )
        queued_result = await queued_executor.result(queued)

        started = asyncio.Event()
        observed = asyncio.Event()

        async def running_runner(task, control):
            started.set()
            await control.cancellation.wait()
            observed.set()
            return make_result(task)

        running_executor = LocalExecutor(
            StageBindingRegistry([("test_stage", running_runner)])
        )
        running = await running_executor.submit(make_task())
        await started.wait()
        await running_executor.cancel(running)
        running_result = await running_executor.result(running)

        assert queued_result.status is StageStatus.CANCELLED
        assert queued_calls == 0
        assert observed.is_set()
        assert running_result.status is StageStatus.CANCELLED

    asyncio.run(scenario())


def test_executor_reports_unknown_stage_and_handle() -> None:
    async def scenario():
        executor = LocalExecutor(StageBindingRegistry([]))
        with pytest.raises(UnknownStageBindingError):
            await executor.submit(make_task())

        unknown = ExecutionHandle("exec_missing", "stage_missing", 1)
        with pytest.raises(UnknownExecutionError):
            await executor.status(unknown)
        with pytest.raises(UnknownExecutionError):
            await executor.result(unknown)
        with pytest.raises(UnknownExecutionError):
            await executor.cancel(unknown)

    asyncio.run(scenario())
