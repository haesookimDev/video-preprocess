"""Network-free tests for Pipeline REST transport mapping."""

from datetime import datetime, timezone

from video_preprocess.api import PipelineHTTPService
from video_preprocess.services import (
    PipelineIdempotencyConflictError,
    PipelineRunNotFoundError,
    PipelineRunSnapshot,
    PipelineRunSubmission,
    PipelineQueryRequest,
    PipelineQueryResult,
    PublicRunStatus,
)


NOW = datetime(2026, 8, 12, tzinfo=timezone.utc).isoformat()


def snapshot() -> PipelineRunSnapshot:
    return PipelineRunSnapshot(
        run_id="run_test",
        status=PublicRunStatus.QUEUED,
        created_at=NOW,
        updated_at=NOW,
        planned_stage_names=("01_probe",),
        current_stage="01_probe",
        current_attempt=1,
        idempotency_key="idem-test",
        request_fingerprint="fingerprint",
    )


class RunService:
    def __init__(self):
        self.created = []
        self.create_error = None
        self.closed = False

    async def create(self, submission):
        self.created.append(submission)
        if self.create_error is not None:
            raise self.create_error
        return snapshot(), True

    def get(self, run_id):
        if run_id == "missing":
            raise PipelineRunNotFoundError
        return snapshot()

    async def cancel(self, run_id):
        return snapshot()

    def artifacts(self, run_id):
        return {"schema_version": "1", "run_id": run_id, "artifacts": {}}

    async def shutdown(self):
        self.closed = True


class QueryService:
    async def query(self, request):
        return PipelineQueryResult(
            run_id=request.run_id,
            query=request.query,
            context="assembled context",
            matches=(),
        )


def test_http_service_maps_create_and_matching_idempotency() -> None:
    runs = RunService()
    service = PipelineHTTPService(runs)
    try:
        submission = PipelineRunSubmission("idem-test", "sample.mp4")
        mismatch_status, mismatch, _ = service.create(
            submission,
            idempotency_key="different",
        )
        status, body, headers = service.create(
            submission,
            idempotency_key="idem-test",
        )
    finally:
        service.close()

    assert mismatch_status == 400
    assert mismatch["error"]["code"] == "INVALID_REQUEST"
    assert status == 202
    assert body["run_id"] == "run_test"
    assert headers["Location"].endswith("/run_test")
    assert runs.created == [submission]
    assert runs.closed is True


def test_http_service_classifies_conflict_and_missing_run() -> None:
    runs = RunService()
    runs.create_error = PipelineIdempotencyConflictError()
    service = PipelineHTTPService(runs)
    try:
        conflict_status, conflict, _ = service.create(
            PipelineRunSubmission("idem-test", "sample.mp4"),
            idempotency_key="idem-test",
        )
        missing_status, missing, _ = service.get("missing")
    finally:
        service.close()

    assert conflict_status == 409
    assert conflict["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert missing_status == 404
    assert missing["error"]["code"] == "RUN_NOT_FOUND"


def test_submission_parser_rejects_unknown_fields_and_invalid_settings() -> None:
    valid = {
        "schema_version": "1",
        "idempotency_key": "idem-test",
        "media_id": "sample.mp4",
        "selection": {"to_stage": "02_scenes"},
        "settings": {"language": "ko"},
    }

    parsed = PipelineRunSubmission.from_dict(valid)

    assert parsed.to_stage == "02_scenes"
    assert parsed.settings.language == "ko"
    for changes in (
        {**valid, "output_root": "/tmp/output"},
        {**valid, "settings": {"keyframes_per_scene": 0}},
        {**valid, "schema_version": "2"},
    ):
        try:
            PipelineRunSubmission.from_dict(changes)
        except (TypeError, ValueError):
            pass
        else:
            raise AssertionError("invalid public request was accepted")


def test_http_service_maps_query_result() -> None:
    service = PipelineHTTPService(
        RunService(),
        query_service=QueryService(),
    )
    try:
        status, body, _ = service.query(
            PipelineQueryRequest("run_test", "질의", top_k=2)
        )
    finally:
        service.close()

    assert status == 200
    assert body == {
        "schema_version": "1",
        "run_id": "run_test",
        "query": "질의",
        "context": "assembled context",
        "matches": [],
    }
