"""Tests for durable asynchronous pipeline run use cases."""

import asyncio
import hashlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from video_preprocess.domain import (
    ArtifactRef,
    Checksum,
    RunStatus,
    StageResult,
    StageStatus,
    StageTask,
)
from video_preprocess.engine import PipelineRunResult, StageExecutionRecord
from video_preprocess.services import (
    EngineRunObservation,
    LocalMediaCatalog,
    LocalPipelineRunRepository,
    MediaNotFoundError,
    PipelineCapacityError,
    PipelineIdempotencyConflictError,
    PipelineRunNotReadyError,
    PipelineRunService,
    PipelineRunSnapshot,
    PipelineRunSubmission,
    PublicRunStatus,
)


def artifact(name: str) -> ArtifactRef:
    payload = name.encode()
    return ArtifactRef(
        artifact_id=name,
        kind="json",
        uri=f"artifact://test/{name}.json",
        media_type="application/json",
        size_bytes=len(payload),
        checksum=Checksum("sha256", hashlib.sha256(payload).hexdigest()),
    )


def stage_record(
    run_id: str,
    stage: str,
    status: StageStatus = StageStatus.SUCCEEDED,
) -> StageExecutionRecord:
    task = StageTask(
        run_id=run_id,
        stage_run_id=f"stage-{stage}",
        attempt=1,
        stage=stage,
        stage_version="1",
        inputs={},
        config={},
        model_bindings={},
        idempotency_key=f"idem-{stage}",
        trace_id="trace-test",
    )
    result = StageResult(
        run_id=run_id,
        stage_run_id=task.stage_run_id,
        attempt=1,
        status=status,
        outputs={stage: artifact(stage)} if status is StageStatus.SUCCEEDED else {},
        reason_code="FAILED" if status is StageStatus.FAILED else None,
        reason="stage failed" if status is StageStatus.FAILED else None,
    )
    return StageExecutionRecord(
        stage=stage,
        task=task,
        handle=None,
        result=result,
        transitions=(),
    )


class ControlledApplication:
    def __init__(self, stage_names=("01_probe", "02_scenes")):
        self.stage_names = stage_names
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = []

    def plan(self, request):
        return SimpleNamespace(stage_names=self.stage_names)

    async def run(self, request, *, cancellation=None):
        self.calls.append(request)
        self.started.set()
        while not self.release.is_set() and not cancellation.cancelled:
            await asyncio.sleep(0)
        status = (
            RunStatus.CANCELLED
            if cancellation.cancelled
            else RunStatus.SUCCEEDED
        )
        records = tuple(
            stage_record(
                request.run_id,
                name,
                (
                    StageStatus.CANCELLED
                    if status is RunStatus.CANCELLED
                    else StageStatus.SUCCEEDED
                ),
            )
            for name in self.stage_names
        )
        return PipelineRunResult(
            run_id=request.run_id,
            status=status,
            stages=records,
            artifacts={"video": artifact("video"), "index": artifact("index")},
            transitions=(),
        )


def make_service(tmp_path: Path, application=None, **options):
    media_root = tmp_path / "media"
    media_root.mkdir(exist_ok=True)
    (media_root / "sample.mp4").write_bytes(b"video")
    repository = LocalPipelineRunRepository(tmp_path / "state")
    service = PipelineRunService(
        application or ControlledApplication(),
        repository,
        LocalMediaCatalog(media_root),
        tmp_path / "runs",
        run_id_factory=lambda: "run_test",
        **options,
    )
    return service, repository


def test_create_runs_shared_application_and_persists_public_result(
    tmp_path: Path,
) -> None:
    async def scenario():
        application = ControlledApplication()
        service, repository = make_service(tmp_path, application)
        submission = PipelineRunSubmission("idem-1", "sample.mp4")

        accepted, created = await service.create(submission)
        assert created is True
        assert accepted.status is PublicRunStatus.QUEUED
        assert accepted.public_dict()["progress"]["planned_stages"] == 2
        with pytest.raises(PipelineRunNotReadyError):
            service.artifacts(accepted.run_id)

        await application.started.wait()
        assert service.get(accepted.run_id).status is PublicRunStatus.RUNNING
        application.release.set()
        completed = await service.wait(accepted.run_id)

        assert completed.status is PublicRunStatus.SUCCEEDED
        assert completed.progress_ratio == 1.0
        assert set(service.artifacts(completed.run_id)["artifacts"]) == {"index"}
        assert repository.load(completed.run_id) == completed
        request = application.calls[0]
        assert request.video_path == (tmp_path / "media" / "sample.mp4")
        assert request.output_root == tmp_path / "runs" / "run_test"

    asyncio.run(scenario())


def test_idempotency_recovers_same_run_and_rejects_changed_request(
    tmp_path: Path,
) -> None:
    async def scenario():
        application = ControlledApplication()
        service, _ = make_service(tmp_path, application)
        submission = PipelineRunSubmission("idem-1", "sample.mp4")
        first, _ = await service.create(submission)
        recovered, created = await service.create(submission)

        assert created is False
        assert recovered.run_id == first.run_id
        with pytest.raises(PipelineIdempotencyConflictError):
            await service.create(
                PipelineRunSubmission(
                    "idem-1", "sample.mp4", max_stage_attempts=2
                )
            )
        application.release.set()
        await service.wait(first.run_id)

    asyncio.run(scenario())


def test_cancel_is_cooperative_and_terminal_snapshot_is_durable(
    tmp_path: Path,
) -> None:
    async def scenario():
        application = ControlledApplication(("01_probe",))
        service, _ = make_service(tmp_path, application)
        accepted, _ = await service.create(
            PipelineRunSubmission("idem-cancel", "sample.mp4")
        )
        await application.started.wait()

        await service.cancel(accepted.run_id)
        cancelled = await service.wait(accepted.run_id)

        assert cancelled.status is PublicRunStatus.CANCELLED
        assert cancelled.failure.code == "CANCELLED"
        assert (await service.cancel(accepted.run_id)) == cancelled

    asyncio.run(scenario())


def test_failed_snapshot_prefers_failed_branch_over_cancelled_peer(
    tmp_path: Path,
) -> None:
    class BranchFailureApplication:
        def plan(self, request):
            return SimpleNamespace(
                stage_names=("01_visual", "02_audio", "03_join")
            )

        async def run(self, request, *, cancellation=None):
            return PipelineRunResult(
                run_id=request.run_id,
                status=RunStatus.FAILED,
                stages=(
                    stage_record(
                        request.run_id,
                        "01_visual",
                        StageStatus.FAILED,
                    ),
                    stage_record(
                        request.run_id,
                        "02_audio",
                        StageStatus.CANCELLED,
                    ),
                ),
                artifacts={},
                transitions=(),
            )

    async def scenario():
        service, _ = make_service(tmp_path, BranchFailureApplication())
        accepted, _ = await service.create(
            PipelineRunSubmission("idem-failed-branch", "sample.mp4")
        )
        failed = await service.wait(accepted.run_id)

        assert failed.status is PublicRunStatus.FAILED
        assert failed.failure.code == "PIPELINE_FAILED"
        assert failed.failure.stage == "01_visual"
        assert failed.failure.message == "stage failed"

    asyncio.run(scenario())


def test_restart_reconciles_non_terminal_snapshot_as_interrupted(
    tmp_path: Path,
) -> None:
    async def create_active():
        application = ControlledApplication()
        service, repository = make_service(tmp_path, application)
        accepted, _ = await service.create(
            PipelineRunSubmission("idem-restart", "sample.mp4")
        )
        await application.started.wait()
        # Simulate process loss by preserving state while stopping this task.
        service._tasks[accepted.run_id].cancel()
        try:
            await service._tasks[accepted.run_id]
        except asyncio.CancelledError:
            pass
        return repository

    repository = asyncio.run(create_active())
    media_root = tmp_path / "media"
    restarted = PipelineRunService(
        ControlledApplication(),
        repository,
        LocalMediaCatalog(media_root),
        tmp_path / "runs",
    )

    snapshot = restarted.get("run_test")
    assert snapshot.status is PublicRunStatus.FAILED
    assert snapshot.failure.code == "RUN_INTERRUPTED"
    assert snapshot.failure.retryable is True


def test_media_catalog_rejects_missing_absolute_and_traversal_paths(
    tmp_path: Path,
) -> None:
    media = tmp_path / "media"
    media.mkdir()
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")
    catalog = LocalMediaCatalog(media)

    for media_id in ("missing.mp4", str(outside), "../outside.mp4"):
        with pytest.raises(MediaNotFoundError, match="not available"):
            catalog.resolve(media_id)


def test_capacity_rejects_second_active_run(tmp_path: Path) -> None:
    async def scenario():
        application = ControlledApplication()
        service, _ = make_service(tmp_path, application)
        first, _ = await service.create(
            PipelineRunSubmission("idem-1", "sample.mp4")
        )
        service.run_id_factory = lambda: "run_second"
        with pytest.raises(PipelineCapacityError):
            await service.create(
                PipelineRunSubmission("idem-2", "sample.mp4")
            )
        application.release.set()
        await service.wait(first.run_id)

    asyncio.run(scenario())


def test_status_projects_engine_progress_without_exposing_paths(
    tmp_path: Path,
) -> None:
    class ProgressReader:
        def read(self, run_id):
            return EngineRunObservation(
                status=RunStatus.RUNNING,
                stage_results=(("01_probe", 1, StageStatus.SUCCEEDED),),
                warnings=("probe warning",),
                artifacts={"metadata": artifact("metadata")},
            )

    async def scenario():
        application = ControlledApplication()
        service, _ = make_service(
            tmp_path,
            application,
            progress_reader=ProgressReader(),
        )
        accepted, _ = await service.create(
            PipelineRunSubmission("idem-progress", "sample.mp4")
        )
        await application.started.wait()

        current = service.get(accepted.run_id)

        assert current.completed_stage_names == ("01_probe",)
        assert current.current_stage == "02_scenes"
        assert current.current_attempt == 1
        assert current.warnings == ("probe warning",)
        assert current.artifacts["metadata"].uri.startswith("artifact://")
        assert str(tmp_path) not in str(current.public_dict())
        application.release.set()
        await service.wait(accepted.run_id)

    asyncio.run(scenario())


def test_repository_prunes_only_old_terminal_control_snapshots(
    tmp_path: Path,
) -> None:
    repository = LocalPipelineRunRepository(
        tmp_path / "state",
        retain_terminal_runs=1,
    )
    artifact_file = tmp_path / "runs" / "run_old" / "artifact.json"
    artifact_file.parent.mkdir(parents=True)
    artifact_file.write_text("preserved", encoding="utf-8")
    base = PipelineRunSnapshot(
        run_id="run_old",
        status=PublicRunStatus.SUCCEEDED,
        created_at="2026-08-12T00:00:00Z",
        updated_at="2026-08-12T00:01:00Z",
        completed_at="2026-08-12T00:01:00Z",
        planned_stage_names=("01_probe",),
        completed_stage_names=("01_probe",),
        idempotency_key="idem-old",
        request_fingerprint="fingerprint-old",
    )
    repository.save(base)
    repository.save(
        replace(
            base,
            run_id="run_new",
            created_at="2026-08-12T00:02:00Z",
            updated_at="2026-08-12T00:03:00Z",
            completed_at="2026-08-12T00:03:00Z",
            idempotency_key="idem-new",
            request_fingerprint="fingerprint-new",
        )
    )

    assert repository.load("run_old") is None
    assert repository.load("run_new") is not None
    assert artifact_file.read_text(encoding="utf-8") == "preserved"
