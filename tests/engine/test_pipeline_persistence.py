"""PipelineEngine persistence and cache-hit integration tests."""

import asyncio
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from video_preprocess.domain import (
    ArtifactRef,
    Checksum,
    ModelExecution,
    RunStatus,
    StageResult,
    StageSpec,
    StageStatus,
)
from video_preprocess.engine import (
    CacheMissReason,
    CacheStatus,
    DAGPlanner,
    EngineInputError,
    EnginePersistenceError,
    ManifestCacheEvaluator,
    PipelineEngine,
    StagePreviewStatus,
    StageLifecycle,
    StageRegistry,
)
from video_preprocess.executors import (
    ExecutionHandle,
    ExecutionState,
    ExecutionStatus,
)
from video_preprocess.storage import (
    ArtifactVerification,
    LocalArtifactStore,
    LocalRunStore,
)


def artifact(name: str) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=f"art-{name}",
        kind="json",
        uri=f"artifact://run-123/{name}.json",
        media_type="application/json",
        size_bytes=len(name),
        checksum=Checksum("sha256", f"checksum-{name}"),
    )


def expected_model() -> ModelExecution:
    return ModelExecution(
        slot="worker",
        provider="local.worker",
        model="worker-model",
        revision="revision-1",
        runtime="worker/1.0",
    )


def plan():
    return DAGPlanner(
        StageRegistry(
            [
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
            ],
            external_inputs=("source",),
        )
    ).plan()


class FakeExecutor:
    def __init__(self, resolver) -> None:
        self.resolver = resolver
        self.tasks = []
        self.tasks_by_execution = {}

    async def submit(self, task):
        self.tasks.append(task)
        handle = ExecutionHandle(
            execution_id=f"execution-{len(self.tasks)}",
            stage_run_id=task.stage_run_id,
            attempt=task.attempt,
        )
        self.tasks_by_execution[handle.execution_id] = task
        return handle

    async def status(self, handle):
        return ExecutionStatus(handle, ExecutionState.SUCCEEDED)

    async def result(self, handle):
        return self.resolver(self.tasks_by_execution[handle.execution_id])

    async def cancel(self, handle):
        return None


def success(task):
    if task.stage == "01_prepare":
        outputs = {"prepared": artifact("prepared")}
        models = ()
    else:
        outputs = {"processed": artifact("processed")}
        models = (expected_model(),)
    return StageResult(
        run_id=task.run_id,
        stage_run_id=task.stage_run_id,
        attempt=task.attempt,
        status=StageStatus.SUCCEEDED,
        outputs=outputs,
        models=models,
    )


class FakeArtifactStore:
    def verify(self, ref):
        return ArtifactVerification(
            exists=True,
            expected_size_bytes=ref.size_bytes,
            actual_size_bytes=ref.size_bytes,
            expected_checksum=ref.checksum,
            actual_checksum=ref.checksum,
        )


class FakeRunStore:
    def __init__(self) -> None:
        self.runs = []
        self.stages = {}
        self.events = []
        self.save_run_error = None

    def save_run(self, manifest):
        if self.save_run_error is not None:
            raise self.save_run_error
        self.runs.append(manifest)
        self.events.append(
            ("run", manifest.status.value, len(manifest.stages))
        )

    def load_stage(self, run_id, reference):
        return self.stages.get((run_id, reference))

    def save_stage(self, manifest):
        key = (manifest.task.run_id, manifest.reference)
        self.stages[key] = manifest
        self.events.append(("stage", manifest.task.stage))


class IncrementingClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        current = self.value
        self.value += timedelta(seconds=1)
        return current


class StaticModelResolver:
    def __init__(self) -> None:
        self.tasks = []

    async def resolve(self, task):
        self.tasks.append(task)
        return (expected_model(),)


def run_engine(
    executor,
    run_store,
    *,
    model_resolver=None,
    force_stages=None,
    stage_configs=None,
):
    engine = PipelineEngine(
        executor,
        run_store=run_store,
        cache_evaluator=ManifestCacheEvaluator(FakeArtifactStore()),
        model_resolver=model_resolver,
        clock=IncrementingClock(),
    )
    return asyncio.run(
        engine.run(
            plan(),
            run_id="run-123",
            trace_id="trace-123",
            artifacts={"source": artifact("source")},
            stage_configs=(
                {"02_process": {"threshold": 0.5}}
                if stage_configs is None
                else stage_configs
            ),
            model_bindings={
                "02_process": {"worker": "worker.default"}
            },
            force_stages=force_stages,
        )
    )


def test_engine_persists_running_stage_and_terminal_manifests_in_order() -> None:
    runs = FakeRunStore()
    result = run_engine(FakeExecutor(success), runs)

    assert result.status is RunStatus.SUCCEEDED
    assert result.manifest == runs.runs[-1]
    assert result.manifest.config == {
        "02_process": {"threshold": 0.5}
    }
    assert result.manifest.model_bindings == {
        "02_process.worker": "worker.default"
    }
    assert runs.events == [
        ("run", "running", 0),
        ("stage", "01_prepare"),
        ("run", "running", 1),
        ("stage", "02_process"),
        ("run", "running", 2),
        ("run", "succeeded", 2),
    ]
    assert all(manifest.cache_key for manifest in runs.stages.values())


def test_second_run_reuses_verified_stage_manifests_without_executor() -> None:
    runs = FakeRunStore()
    run_engine(FakeExecutor(success), runs)
    executor = FakeExecutor(success)
    resolver = StaticModelResolver()

    result = run_engine(
        executor,
        runs,
        model_resolver=resolver,
    )

    assert executor.tasks == []
    assert [record.state for record in result.stages] == [
        StageLifecycle.CACHED,
        StageLifecycle.CACHED,
    ]
    assert all(record.from_cache for record in result.stages)
    assert all(
        record.cache_decision.status is CacheStatus.HIT
        for record in result.stages
    )
    assert [task.stage for task in resolver.tasks] == ["02_process"]


def test_preview_reuses_hits_without_writing_or_submitting() -> None:
    runs = FakeRunStore()
    run_engine(FakeExecutor(success), runs)
    prior_events = list(runs.events)
    executor = FakeExecutor(success)
    engine = PipelineEngine(
        executor,
        run_store=runs,
        cache_evaluator=ManifestCacheEvaluator(FakeArtifactStore()),
        model_resolver=StaticModelResolver(),
    )

    result = asyncio.run(
        engine.preview(
            plan(),
            run_id="run-123",
            trace_id="trace-preview",
            artifacts={"source": artifact("source")},
            stage_configs={"02_process": {"threshold": 0.5}},
            model_bindings={
                "02_process": {"worker": "worker.default"}
            },
        )
    )

    assert [record.status for record in result.stages] == [
        StagePreviewStatus.HIT,
        StagePreviewStatus.HIT,
    ]
    assert executor.tasks == []
    assert runs.events == prior_events
    assert set(result.artifacts) == {"source", "prepared", "processed"}


def test_preview_blocks_downstream_after_miss_or_force() -> None:
    empty_runs = FakeRunStore()
    engine = PipelineEngine(
        FakeExecutor(success),
        run_store=empty_runs,
        cache_evaluator=ManifestCacheEvaluator(FakeArtifactStore()),
    )

    missing = asyncio.run(
        engine.preview(
            plan(),
            run_id="run-123",
            trace_id="trace-preview",
            artifacts={"source": artifact("source")},
            stage_configs={"02_process": {"threshold": 0.5}},
            model_bindings={
                "02_process": {"worker": "worker.default"}
            },
        )
    )
    assert [record.status for record in missing.stages] == [
        StagePreviewStatus.MISS,
        StagePreviewStatus.BLOCKED,
    ]
    assert missing.stages[0].cache_decision.misses[0].reason is (
        CacheMissReason.MANIFEST_NOT_FOUND
    )
    assert missing.stages[1].blocked_inputs == ("prepared",)

    populated_runs = FakeRunStore()
    run_engine(FakeExecutor(success), populated_runs)
    forced_engine = PipelineEngine(
        FakeExecutor(success),
        run_store=populated_runs,
        cache_evaluator=ManifestCacheEvaluator(FakeArtifactStore()),
        model_resolver=StaticModelResolver(),
    )
    forced = asyncio.run(
        forced_engine.preview(
            plan(),
            run_id="run-123",
            trace_id="trace-preview",
            artifacts={"source": artifact("source")},
            stage_configs={"02_process": {"threshold": 0.5}},
            model_bindings={
                "02_process": {"worker": "worker.default"}
            },
            force_stages={"01_prepare"},
        )
    )
    assert [record.status for record in forced.stages] == [
        StagePreviewStatus.FORCED,
        StagePreviewStatus.BLOCKED,
    ]


def test_model_stage_executes_when_effective_model_cannot_be_resolved() -> None:
    runs = FakeRunStore()
    run_engine(FakeExecutor(success), runs)
    executor = FakeExecutor(success)

    result = run_engine(executor, runs)

    assert [task.stage for task in executor.tasks] == ["02_process"]
    assert result.stages[0].from_cache
    assert not result.stages[1].from_cache
    assert {
        miss.reason for miss in result.stages[1].cache_decision.misses
    } >= {CacheMissReason.EFFECTIVE_MODELS_UNAVAILABLE}


def test_force_stage_executes_target_but_content_equal_downstream_hits() -> None:
    runs = FakeRunStore()
    run_engine(FakeExecutor(success), runs)
    executor = FakeExecutor(success)

    result = run_engine(
        executor,
        runs,
        model_resolver=StaticModelResolver(),
        force_stages={"01_prepare"},
    )

    assert [task.stage for task in executor.tasks] == ["01_prepare"]
    assert result.stages[0].cache_decision.status is CacheStatus.FORCED
    assert result.stages[1].from_cache


def test_config_change_misses_only_affected_stage() -> None:
    runs = FakeRunStore()
    run_engine(FakeExecutor(success), runs)
    executor = FakeExecutor(success)

    result = run_engine(
        executor,
        runs,
        model_resolver=StaticModelResolver(),
        stage_configs={"02_process": {"threshold": 0.75}},
    )

    assert result.stages[0].from_cache
    assert [task.stage for task in executor.tasks] == ["02_process"]
    assert CacheMissReason.CONFIG_CHANGED in {
        miss.reason for miss in result.stages[1].cache_decision.misses
    }


@pytest.mark.parametrize(
    ("stage_status", "run_status"),
    [
        (StageStatus.FAILED, RunStatus.FAILED),
        (StageStatus.CANCELLED, RunStatus.CANCELLED),
    ],
)
def test_failed_and_cancelled_attempts_are_persisted_for_diagnostics(
    stage_status: StageStatus,
    run_status: RunStatus,
) -> None:
    def terminal_result(task):
        return StageResult(
            run_id=task.run_id,
            stage_run_id=task.stage_run_id,
            attempt=task.attempt,
            status=stage_status,
            reason_code="TEST_TERMINAL",
            reason="test terminal result",
        )

    runs = FakeRunStore()
    result = run_engine(FakeExecutor(terminal_result), runs)

    assert result.status is run_status
    assert result.manifest.status is run_status
    assert len(runs.stages) == 1
    saved_stage = next(iter(runs.stages.values()))
    assert saved_stage.result.status is stage_status
    assert saved_stage.result.reason_code == "TEST_TERMINAL"


def test_force_stage_requires_cache_configuration_and_planned_name() -> None:
    executor = FakeExecutor(success)

    with pytest.raises(EngineInputError, match="cache evaluator"):
        asyncio.run(
            PipelineEngine(executor).run(
                plan(),
                run_id="run-123",
                trace_id="trace-123",
                artifacts={"source": artifact("source")},
                model_bindings={
                    "02_process": {"worker": "worker.default"}
                },
                force_stages={"01_prepare"},
            )
        )

    with pytest.raises(EngineInputError, match="unplanned"):
        run_engine(
            executor,
            FakeRunStore(),
            force_stages={"missing"},
        )


def test_run_store_failure_is_explicit_and_prevents_submission() -> None:
    runs = FakeRunStore()
    runs.save_run_error = OSError("disk path")
    executor = FakeExecutor(success)

    with pytest.raises(EnginePersistenceError, match="run manifest"):
        run_engine(executor, runs)

    assert executor.tasks == []


def test_local_stores_support_persisted_cache_resume(tmp_path: Path) -> None:
    artifacts = LocalArtifactStore(tmp_path, namespace="run-123")
    pending = artifacts.put(
        io.BytesIO(b"source"),
        artifact_id="source",
        relative_path="source.json",
        kind="json",
        media_type="application/json",
    )
    source = artifacts.publish(pending)
    runs = LocalRunStore(tmp_path, artifacts)
    single_plan = DAGPlanner(
        StageRegistry(
            [
                StageSpec(
                    name="01_copy",
                    stage_version="1.0.0",
                    required_inputs=("source",),
                    outputs=("copy",),
                )
            ],
            external_inputs=("source",),
        )
    ).plan()

    def copy_result(task):
        return StageResult(
            run_id=task.run_id,
            stage_run_id=task.stage_run_id,
            attempt=task.attempt,
            status=StageStatus.SUCCEEDED,
            outputs={"copy": source},
        )

    first_executor = FakeExecutor(copy_result)
    first_engine = PipelineEngine(
        first_executor,
        run_store=runs,
        cache_evaluator=ManifestCacheEvaluator(artifacts),
    )
    first = asyncio.run(
        first_engine.run(
            single_plan,
            run_id="run-123",
            trace_id="trace-first",
            artifacts={"source": source},
        )
    )
    second_executor = FakeExecutor(copy_result)
    second_engine = PipelineEngine(
        second_executor,
        run_store=runs,
        cache_evaluator=ManifestCacheEvaluator(artifacts),
    )
    second = asyncio.run(
        second_engine.run(
            single_plan,
            run_id="run-123",
            trace_id="trace-second",
            artifacts={"source": source},
        )
    )

    assert len(first_executor.tasks) == 1
    assert first.manifest.status is RunStatus.SUCCEEDED
    assert second_executor.tasks == []
    assert second.stages[0].state is StageLifecycle.CACHED
    assert runs.load_run("run-123") == second.manifest
