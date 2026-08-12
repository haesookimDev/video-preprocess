"""Tests for dependency-ready PipelineEngine artifact orchestration."""

import asyncio

import pytest

from video_preprocess.domain import (
    ArtifactRef,
    Checksum,
    RunStatus,
    StageResult,
    StageSpec,
    StageStatus,
)
from video_preprocess.engine import (
    DAGPlanner,
    EngineInputError,
    PipelineEngine,
    RetryPolicy,
    RunStateMachine,
    StageLifecycle,
    StageRegistry,
    StageStateMachine,
    StateTransitionError,
)
from video_preprocess.executors import (
    CancellationToken,
    ExecutionHandle,
    ExecutionState,
    ExecutionStatus,
    LocalExecutor,
    StageBindingRegistry,
)


def artifact(name: str) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=f"art_{name}",
        kind="json",
        uri=f"artifact://run_123/{name}.json",
        media_type="application/json",
        size_bytes=len(name),
        checksum=Checksum("sha256", f"checksum_{name}"),
    )


def pipeline_plan():
    specs = [
        StageSpec(
            name="01_prepare",
            stage_version="1.0.0",
            required_inputs=("source",),
            outputs=("prepared",),
        ),
        StageSpec(
            name="02_process",
            stage_version="1.0.0",
            dependencies=("01_prepare",),
            required_inputs=("prepared",),
            outputs=("processed",),
            model_slots=("worker",),
        ),
        StageSpec(
            name="03_finish",
            stage_version="1.0.0",
            dependencies=("02_process",),
            required_inputs=("processed",),
            outputs=("final",),
        ),
    ]
    return DAGPlanner(
        StageRegistry(specs, external_inputs=("source",))
    )


def branching_plan():
    specs = [
        StageSpec(
            name="01_visual",
            stage_version="1.0.0",
            required_inputs=("source",),
            outputs=("visual",),
        ),
        StageSpec(
            name="02_audio",
            stage_version="1.0.0",
            required_inputs=("source",),
            outputs=("audio",),
        ),
        StageSpec(
            name="03_join",
            stage_version="1.0.0",
            dependencies=("01_visual", "02_audio"),
            required_inputs=("visual", "audio"),
            outputs=("joined",),
        ),
    ]
    return DAGPlanner(
        StageRegistry(specs, external_inputs=("source",))
    ).plan()


class FakeExecutor:
    def __init__(self, resolver, *, submit_error=None, result_error=None):
        self.resolver = resolver
        self.submit_error = submit_error
        self.result_error = result_error
        self.tasks = []
        self.controls = []
        self.by_execution_id = {}

    async def submit(self, task, *, control=None):
        if self.submit_error is not None:
            raise self.submit_error
        self.tasks.append(task)
        self.controls.append(control)
        handle = ExecutionHandle(
            execution_id=f"exec_{len(self.tasks)}",
            stage_run_id=task.stage_run_id,
            attempt=task.attempt,
        )
        self.by_execution_id[handle.execution_id] = task
        return handle

    async def status(self, handle):
        return ExecutionStatus(handle, ExecutionState.SUCCEEDED)

    async def result(self, handle):
        if self.result_error is not None:
            raise self.result_error
        return self.resolver(self.by_execution_id[handle.execution_id])

    async def cancel(self, handle):
        return None


def success_result(task, *, status=StageStatus.SUCCEEDED, outputs=None):
    return StageResult(
        run_id=task.run_id,
        stage_run_id=task.stage_run_id,
        attempt=task.attempt,
        status=status,
        outputs={} if outputs is None else outputs,
    )


def default_resolver(task):
    output_names = {
        "01_prepare": "prepared",
        "02_process": "processed",
        "03_finish": "final",
    }
    name = output_names[task.stage]
    return success_result(task, outputs={name: artifact(name)})


def run_engine(executor, plan=None, **overrides):
    planner = pipeline_plan()
    selected_plan = planner.plan() if plan is None else plan
    kwargs = {
        "run_id": "run_123",
        "trace_id": "trace_123",
        "artifacts": {"source": artifact("source")},
        "stage_configs": {"02_process": {"threshold": 0.5}},
        "model_bindings": {"02_process": {"worker": "worker.default"}},
    }
    kwargs.update(overrides)
    return asyncio.run(PipelineEngine(executor).run(selected_plan, **kwargs))


def test_engine_builds_tasks_and_passes_outputs_to_downstream_stage() -> None:
    executor = FakeExecutor(default_resolver)

    result = run_engine(executor)

    assert result.status is RunStatus.SUCCEEDED
    assert result.transitions == (RunStatus.PENDING, RunStatus.RUNNING, RunStatus.SUCCEEDED)
    assert [record.stage for record in result.stages] == [
        "01_prepare",
        "02_process",
        "03_finish",
    ]
    assert all(
        record.transitions
        == (
            StageLifecycle.PENDING,
            StageLifecycle.QUEUED,
            StageLifecycle.RUNNING,
            StageLifecycle.SUCCEEDED,
        )
        for record in result.stages
    )
    assert executor.tasks[0].inputs == {"source": artifact("source")}
    assert executor.tasks[1].inputs == {"prepared": artifact("prepared")}
    assert executor.tasks[1].config == {"threshold": 0.5}
    assert executor.tasks[1].model_bindings == {
        "worker": "worker.default"
    }
    assert executor.tasks[2].inputs == {"processed": artifact("processed")}
    assert result.artifacts["final"] == artifact("final")


def test_engine_overlaps_ready_branches_and_waits_before_join() -> None:
    async def scenario():
        branch_started = set()
        both_started = asyncio.Event()
        active_branches = 0
        peak_branches = 0
        join_inputs = []

        async def runner(task):
            nonlocal active_branches, peak_branches
            if task.stage in {"01_visual", "02_audio"}:
                active_branches += 1
                peak_branches = max(peak_branches, active_branches)
                branch_started.add(task.stage)
                if len(branch_started) == 2:
                    both_started.set()
                await both_started.wait()
                if task.stage == "01_visual":
                    await asyncio.sleep(0.01)
                output_name = (
                    "visual" if task.stage == "01_visual" else "audio"
                )
                active_branches -= 1
                return success_result(
                    task,
                    outputs={output_name: artifact(output_name)},
                )
            assert active_branches == 0
            join_inputs.append(dict(task.inputs))
            return success_result(
                task,
                outputs={"joined": artifact("joined")},
            )

        executor = LocalExecutor(
            StageBindingRegistry(
                [(name, runner) for name in branching_plan().stage_names]
            ),
            max_concurrency=2,
        )
        result = await asyncio.wait_for(
            PipelineEngine(executor).run(
                branching_plan(),
                run_id="run_123",
                trace_id="trace_123",
                artifacts={"source": artifact("source")},
            ),
            timeout=1,
        )

        assert result.status is RunStatus.SUCCEEDED
        assert peak_branches == 2
        assert [record.stage for record in result.stages] == [
            "01_visual",
            "02_audio",
            "03_join",
        ]
        assert join_inputs == [
            {"visual": artifact("visual"), "audio": artifact("audio")}
        ]

    asyncio.run(scenario())


def test_branch_failure_cancels_peer_and_never_submits_join() -> None:
    async def scenario():
        visual_started = asyncio.Event()
        called = []

        async def runner(task, control):
            called.append(task.stage)
            if task.stage == "01_visual":
                visual_started.set()
                await control.cancellation.wait()
                return success_result(
                    task,
                    outputs={"visual": artifact("visual")},
                )
            if task.stage == "02_audio":
                await visual_started.wait()
                return StageResult(
                    run_id=task.run_id,
                    stage_run_id=task.stage_run_id,
                    attempt=task.attempt,
                    status=StageStatus.FAILED,
                    reason_code="AUDIO_FAILED",
                    reason="audio branch failed",
                )
            raise AssertionError("join must not run after a branch failure")

        executor = LocalExecutor(
            StageBindingRegistry(
                [(name, runner) for name in branching_plan().stage_names]
            ),
            max_concurrency=2,
        )
        result = await asyncio.wait_for(
            PipelineEngine(executor).run(
                branching_plan(),
                run_id="run_123",
                trace_id="trace_123",
                artifacts={"source": artifact("source")},
            ),
            timeout=1,
        )

        assert result.status is RunStatus.FAILED
        assert called == ["01_visual", "02_audio"]
        assert [record.stage for record in result.stages] == [
            "01_visual",
            "02_audio",
        ]
        assert result.stages[0].result.status is StageStatus.CANCELLED
        assert result.stages[0].result.reason_code == "ENGINE_CANCELLED"
        assert result.stages[1].result.reason_code == "AUDIO_FAILED"

    asyncio.run(scenario())


def test_external_cancellation_stops_all_active_branches_before_join() -> None:
    async def scenario():
        started = set()
        both_started = asyncio.Event()
        called = []

        async def runner(task, control):
            called.append(task.stage)
            started.add(task.stage)
            if len(started) == 2:
                both_started.set()
            await control.cancellation.wait()
            output_name = "visual" if task.stage == "01_visual" else "audio"
            return success_result(
                task,
                outputs={output_name: artifact(output_name)},
            )

        executor = LocalExecutor(
            StageBindingRegistry(
                [(name, runner) for name in branching_plan().stage_names]
            ),
            max_concurrency=2,
        )
        cancellation = CancellationToken()
        running = asyncio.create_task(
            PipelineEngine(executor).run(
                branching_plan(),
                run_id="run_123",
                trace_id="trace_123",
                artifacts={"source": artifact("source")},
                cancellation=cancellation,
            )
        )
        await both_started.wait()
        cancellation.cancel()
        result = await asyncio.wait_for(running, timeout=1)

        assert result.status is RunStatus.CANCELLED
        assert called == ["01_visual", "02_audio"]
        assert [record.result.status for record in result.stages] == [
            StageStatus.CANCELLED,
            StageStatus.CANCELLED,
        ]

    asyncio.run(scenario())


def test_engine_task_identity_and_idempotency_are_deterministic() -> None:
    first_executor = FakeExecutor(default_resolver)
    second_executor = FakeExecutor(default_resolver)

    run_engine(first_executor)
    run_engine(second_executor)

    assert [task.stage_run_id for task in first_executor.tasks] == [
        task.stage_run_id for task in second_executor.tasks
    ]
    assert [task.idempotency_key for task in first_executor.tasks] == [
        task.idempotency_key for task in second_executor.tasks
    ]
    assert all(task.idempotency_key.startswith("stage_") for task in first_executor.tasks)

    changed_executor = FakeExecutor(default_resolver)
    run_engine(
        changed_executor,
        stage_configs={"02_process": {"threshold": 0.75}},
    )
    assert (
        first_executor.tasks[1].idempotency_key
        != changed_executor.tasks[1].idempotency_key
    )


def test_partial_plan_uses_boundary_artifact_without_running_ancestor() -> None:
    planner = pipeline_plan()
    executor = FakeExecutor(default_resolver)
    plan = planner.plan(stage="02_process")

    result = run_engine(
        executor,
        plan,
        artifacts={"prepared": artifact("prepared")},
    )

    assert result.status is RunStatus.SUCCEEDED
    assert [task.stage for task in executor.tasks] == ["02_process"]
    assert executor.tasks[0].inputs == {"prepared": artifact("prepared")}


def test_engine_rejects_missing_boundary_before_submission() -> None:
    executor = FakeExecutor(default_resolver)

    with pytest.raises(EngineInputError, match="boundary"):
        run_engine(executor, artifacts={})

    assert executor.tasks == []


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"stage_configs": {"missing": {}}}, "unplanned stage"),
        ({"model_bindings": {}}, "missing worker"),
        (
            {
                "model_bindings": {
                    "02_process": {
                        "worker": "worker.default",
                        "extra": "extra.default",
                    }
                }
            },
            "unexpected extra",
        ),
        ({"attempts": {"02_process": 0}}, "positive integer"),
    ],
)
def test_engine_validates_config_binding_and_attempt_scope(
    overrides,
    message,
) -> None:
    executor = FakeExecutor(default_resolver)

    with pytest.raises(EngineInputError, match=message):
        run_engine(executor, **overrides)

    assert executor.tasks == []


def test_engine_validates_all_config_payloads_before_submission() -> None:
    executor = FakeExecutor(default_resolver)

    with pytest.raises(EngineInputError, match="03_finish"):
        run_engine(
            executor,
            stage_configs={
                "02_process": {"threshold": 0.5},
                "03_finish": {"invalid": object()},
            },
        )

    assert executor.tasks == []


@pytest.mark.parametrize(
    ("terminal_status", "run_status", "lifecycle"),
    [
        (StageStatus.FAILED, RunStatus.FAILED, StageLifecycle.FAILED),
        (StageStatus.CANCELLED, RunStatus.CANCELLED, StageLifecycle.CANCELLED),
    ],
)
def test_engine_stops_after_failed_or_cancelled_stage(
    terminal_status,
    run_status,
    lifecycle,
) -> None:
    def resolver(task):
        if task.stage == "02_process":
            return success_result(task, status=terminal_status)
        return default_resolver(task)

    executor = FakeExecutor(resolver)

    result = run_engine(executor)

    assert result.status is run_status
    assert [task.stage for task in executor.tasks] == [
        "01_prepare",
        "02_process",
    ]
    assert result.stages[-1].state is lifecycle


def test_skipped_stage_can_publish_sentinel_output_and_continue() -> None:
    def resolver(task):
        if task.stage == "01_prepare":
            return success_result(
                task,
                status=StageStatus.SKIPPED,
                outputs={"prepared": artifact("prepared")},
            )
        return default_resolver(task)

    executor = FakeExecutor(resolver)

    result = run_engine(executor)

    assert result.status is RunStatus.SUCCEEDED
    assert result.stages[0].state is StageLifecycle.SKIPPED
    assert len(executor.tasks) == 3


def test_missing_upstream_output_fails_next_stage_without_submitting_it() -> None:
    def resolver(task):
        if task.stage == "01_prepare":
            return success_result(task)
        return default_resolver(task)

    executor = FakeExecutor(resolver)

    result = run_engine(executor)

    assert result.status is RunStatus.FAILED
    assert [task.stage for task in executor.tasks] == ["01_prepare"]
    assert result.stages[-1].stage == "02_process"
    assert result.stages[-1].handle is None
    assert result.stages[-1].result.reason_code == "MISSING_REQUIRED_INPUT"
    assert result.stages[-1].transitions == (
        StageLifecycle.PENDING,
        StageLifecycle.FAILED,
    )


def test_undeclared_stage_output_is_normalized_and_stops_run() -> None:
    def resolver(task):
        return success_result(
            task,
            outputs={"undeclared": artifact("undeclared")},
        )

    executor = FakeExecutor(resolver)

    result = run_engine(executor)

    assert result.status is RunStatus.FAILED
    assert result.stages[0].result.reason_code == "UNDECLARED_STAGE_OUTPUT"
    assert "undeclared" not in result.artifacts
    assert len(executor.tasks) == 1


@pytest.mark.parametrize(
    ("executor", "reason_code"),
    [
        (
            FakeExecutor(default_resolver, submit_error=RuntimeError("submit")),
            "EXECUTOR_SUBMIT_FAILED",
        ),
        (
            FakeExecutor(default_resolver, result_error=RuntimeError("result")),
            "EXECUTOR_RESULT_FAILED",
        ),
    ],
)
def test_engine_normalizes_executor_infrastructure_errors(
    executor,
    reason_code,
) -> None:
    result = run_engine(executor)

    assert result.status is RunStatus.FAILED
    assert result.stages[0].result.reason_code == reason_code


def test_engine_retries_only_classified_failures_with_new_attempt() -> None:
    def resolver(task):
        if task.attempt == 1:
            return StageResult(
                run_id=task.run_id,
                stage_run_id=task.stage_run_id,
                attempt=task.attempt,
                status=StageStatus.FAILED,
                reason_code="EXECUTOR_RESULT_FAILED",
                reason="transient result transport failure",
            )
        return default_resolver(task)

    planner = pipeline_plan()
    executor = FakeExecutor(resolver)
    result = run_engine(
        executor,
        planner.plan(stage="01_prepare"),
        retry_policy=RetryPolicy(max_attempts=2),
        stage_configs={},
        model_bindings={},
    )

    assert result.status is RunStatus.SUCCEEDED
    assert [record.task.attempt for record in result.stages] == [1, 2]
    assert result.stages[0].result.status is StageStatus.FAILED
    assert result.stages[1].result.status is StageStatus.SUCCEEDED
    assert executor.tasks[0].stage_run_id == executor.tasks[1].stage_run_id
    assert (
        executor.tasks[0].idempotency_key
        != executor.tasks[1].idempotency_key
    )


def test_engine_does_not_retry_permanent_stage_failure() -> None:
    def resolver(task):
        return StageResult(
            run_id=task.run_id,
            stage_run_id=task.stage_run_id,
            attempt=task.attempt,
            status=StageStatus.FAILED,
            reason_code="INVALID_REQUEST",
            reason="permanent failure",
        )

    result = run_engine(
        FakeExecutor(resolver),
        pipeline_plan().plan(stage="01_prepare"),
        retry_policy=RetryPolicy(max_attempts=3),
        stage_configs={},
        model_bindings={},
    )

    assert result.status is RunStatus.FAILED
    assert len(result.stages) == 1


def test_engine_times_out_cooperative_stage_and_retries() -> None:
    async def scenario():
        async def runner(task, control):
            if task.attempt == 1:
                await control.cancellation.wait()
            return default_resolver(task)

        executor = LocalExecutor(
            StageBindingRegistry([("01_prepare", runner)])
        )
        result = await PipelineEngine(executor).run(
            pipeline_plan().plan(stage="01_prepare"),
            run_id="run_123",
            trace_id="trace_123",
            artifacts={"source": artifact("source")},
            stage_timeouts={"01_prepare": 0.01},
            retry_policy=RetryPolicy(max_attempts=2),
        )

        assert result.status is RunStatus.SUCCEEDED
        assert [record.result.reason_code for record in result.stages] == [
            "STAGE_TIMEOUT",
            None,
        ]
        assert [record.task.attempt for record in result.stages] == [1, 2]

    asyncio.run(scenario())


def test_engine_propagates_external_cancellation_to_running_stage() -> None:
    async def scenario():
        started = asyncio.Event()

        async def runner(task, control):
            started.set()
            await control.cancellation.wait()
            return default_resolver(task)

        executor = LocalExecutor(
            StageBindingRegistry([("01_prepare", runner)])
        )
        cancellation = CancellationToken()
        running = asyncio.create_task(
            PipelineEngine(executor).run(
                pipeline_plan().plan(stage="01_prepare"),
                run_id="run_123",
                trace_id="trace_123",
                artifacts={"source": artifact("source")},
                cancellation=cancellation,
            )
        )
        await started.wait()
        cancellation.cancel()
        result = await running

        assert result.status is RunStatus.CANCELLED
        assert result.stages[-1].result.reason_code == "ENGINE_CANCELLED"
        assert result.stages[-1].state is StageLifecycle.CANCELLED

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "stage_timeouts",
    [
        {"missing": 1.0},
        {"01_prepare": 0},
    ],
)
def test_engine_validates_stage_timeout_scope(stage_timeouts) -> None:
    with pytest.raises(EngineInputError):
        run_engine(
            FakeExecutor(default_resolver),
            pipeline_plan().plan(stage="01_prepare"),
            stage_timeouts=stage_timeouts,
        )


def test_stage_and_run_state_machines_reject_invalid_transitions() -> None:
    stage = StageStateMachine()
    stage.transition(StageLifecycle.QUEUED)
    stage.transition(StageLifecycle.RUNNING)
    stage.transition(StageLifecycle.SUCCEEDED)
    with pytest.raises(StateTransitionError, match="invalid Stage"):
        stage.transition(StageLifecycle.FAILED)

    cached = StageStateMachine()
    cached.transition(StageLifecycle.CACHED)
    assert cached.state is StageLifecycle.CACHED
    assert cached.state.terminal
    with pytest.raises(StateTransitionError, match="invalid Stage"):
        cached.transition(StageLifecycle.RUNNING)

    run = RunStateMachine()
    with pytest.raises(StateTransitionError, match="invalid run"):
        run.transition(RunStatus.SUCCEEDED)
