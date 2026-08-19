"""Tests for query-guided two-pass reprocessing planning."""

import asyncio
import hashlib
from dataclasses import replace

import pytest

from video_preprocess.domain import ArtifactRef, Checksum
from video_preprocess.engine import DAGPlanner, create_default_registry
from video_preprocess.services import (
    PipelineQueryResult,
    PipelineReprocessingSubmission,
    QueryMatch,
    QueryReprocessingApplicationService,
    ReprocessingInputError,
    ReprocessingNoCandidatesError,
    ReprocessingSource,
    ReprocessingSourceNotReadyError,
    VISUAL_DETAIL_PROFILE,
)


REQUIRED_SOURCE_ARTIFACTS = (
    "audio_events",
    "captions",
    "diarization",
    "embedded_text",
    "keyframe_images",
    "keyframes",
    "metadata",
    "ocr",
    "scenes",
    "search_index",
    "timeline",
    "transcript",
    "video",
)


def artifact(name: str, payload: bytes | None = None) -> ArtifactRef:
    body = name.encode("utf-8") if payload is None else payload
    return ArtifactRef(
        artifact_id=name,
        kind="video" if name == "video" else "json",
        uri=f"artifact://source/{name}",
        media_type=(
            "video/mp4" if name == "video" else "application/json"
        ),
        size_bytes=len(body),
        checksum=Checksum("sha256", hashlib.sha256(body).hexdigest()),
    )


def source(**changes: ArtifactRef) -> ReprocessingSource:
    artifacts = {name: artifact(name) for name in REQUIRED_SOURCE_ARTIFACTS}
    artifacts.update(changes)
    return ReprocessingSource("run_source", artifacts)


def match(rank: int, scene_id: int) -> QueryMatch:
    return QueryMatch(
        rank=rank,
        scene_id=scene_id,
        start_sec=float((scene_id - 1) * 10),
        end_sec=float(scene_id * 10),
        score=1 / (60 + rank),
        text=f"scene {scene_id}",
        keyword_rank=rank,
        semantic_rank=rank,
        semantic_similarity=0.9 - rank / 100,
        reasons=("keyword", "semantic"),
    )


class SourceResolver:
    def __init__(self, value: ReprocessingSource):
        self.value = value
        self.calls = []

    def resolve(self, run_id):
        self.calls.append(run_id)
        return self.value


class QueryService:
    def __init__(self, matches=(match(1, 2), match(2, 3), match(3, 1))):
        self.matches = matches
        self.calls = []

    async def query(self, request):
        self.calls.append(request)
        return PipelineQueryResult(
            run_id=request.run_id,
            query=request.query,
            normalized_query="대시보드 설명",
            context="unused planning context",
            context_stats={},
            matches=self.matches,
        )


def service(query=None, source_value=None):
    queries = query or QueryService()
    resolver = SourceResolver(source_value or source())
    planner = QueryReprocessingApplicationService(
        DAGPlanner(create_default_registry()),
        queries,
        resolver,
    )
    return planner, queries, resolver


def test_reprocessing_plan_captures_ranked_scenes_stage_scope_and_provenance():
    planner, queries, resolver = service()
    submission = PipelineReprocessingSubmission(
        "idem-detail-1",
        "run_source",
        "대시보드 설명",
        max_scenes=2,
        min_similarity=0.5,
    )

    plan = asyncio.run(planner.plan(submission))
    payload = plan.to_dict()

    assert resolver.calls == ["run_source"]
    query_request = queries.calls[0]
    assert query_request.run_id == "run_source"
    assert query_request.top_k == 2
    assert query_request.min_similarity == 0.5
    assert query_request.adjacent_scenes == 0
    assert query_request.max_context_tokens == 128
    assert plan.selected_scene_ids == (2, 3)
    assert [stage.name for stage in plan.stages] == [
        "03_keyframes",
        "08_captions",
        "08_ocr",
        "09_timeline",
        "10_index",
        "11_context",
    ]
    assert [stage.scope for stage in plan.stages] == [
        "selected-scenes",
        "selected-scenes",
        "selected-scenes",
        "full-materialization",
        "full-materialization",
        "full-materialization",
    ]
    assert plan.boundary_inputs == (
        "audio_events",
        "diarization",
        "embedded_text",
        "metadata",
        "scenes",
        "transcript",
        "video",
    )
    assert tuple(sorted(plan.source_artifacts)) == REQUIRED_SOURCE_ARTIFACTS
    assert payload["quality_profile"]["name"] == VISUAL_DETAIL_PROFILE
    assert payload["quality_profile"]["settings_overrides"] == {
        "keyframes_per_scene": 3,
        "ocr_mode": "all",
    }
    assert payload["execution"]["ready"] is False
    assert payload["execution"]["pending_capabilities"] == [
        "source-artifact-import-v1",
        "selected-scene-keyframe-overlay-v1",
        "selected-scene-caption-overlay-v1",
        "selected-scene-ocr-overlay-v1",
    ]
    assert str(plan.source_artifacts["video"].uri) in str(payload)
    assert "/Users/" not in str(payload)


def test_plan_identity_is_stable_and_changes_with_source_content():
    submission = PipelineReprocessingSubmission(
        "idem-first",
        "run_source",
        "대시보드 설명",
        max_scenes=2,
    )
    first_service, _, _ = service()
    same_service, _, _ = service()
    changed_service, _, _ = service(
        source_value=source(timeline=artifact("timeline", b"changed"))
    )

    first = asyncio.run(first_service.plan(submission))
    same = asyncio.run(
        same_service.plan(replace(submission, idempotency_key="idem-retry"))
    )
    changed = asyncio.run(changed_service.plan(submission))

    assert first.request_fingerprint == same.request_fingerprint
    assert first.plan_fingerprint == same.plan_fingerprint
    assert first.plan_id == same.plan_id
    assert changed.plan_fingerprint != first.plan_fingerprint
    assert changed.plan_id != first.plan_id


def test_reprocessing_plan_rejects_missing_provenance_and_empty_result():
    incomplete = source()
    missing = dict(incomplete.artifacts)
    del missing["captions"]
    missing_service, queries, _ = service(
        source_value=ReprocessingSource("run_source", missing)
    )
    submission = PipelineReprocessingSubmission(
        "idem-missing",
        "run_source",
        "대시보드 설명",
    )

    with pytest.raises(
        ReprocessingSourceNotReadyError,
        match="captions",
    ):
        asyncio.run(missing_service.plan(submission))
    assert queries.calls == []

    empty_service, _, _ = service(query=QueryService(matches=()))
    with pytest.raises(ReprocessingNoCandidatesError):
        asyncio.run(empty_service.plan(submission))

    class NoAnswerQueryService(QueryService):
        async def query(self, request):
            result = await super().query(request)
            return replace(result, no_answer=True)

    no_answer_service, _, _ = service(
        query=NoAnswerQueryService(matches=(match(1, 2),))
    )
    with pytest.raises(ReprocessingNoCandidatesError):
        asyncio.run(no_answer_service.plan(submission))


def test_reprocessing_submission_is_closed_versioned_and_idempotent():
    data = {
        "schema_version": "1",
        "idempotency_key": "idem-request",
        "query": "설명 장면",
        "quality_profile": VISUAL_DETAIL_PROFILE,
        "max_scenes": 4,
        "min_similarity": 0.6,
    }

    parsed = PipelineReprocessingSubmission.from_dict("run_source", data)

    assert parsed.to_dict() == {**data, "source_run_id": "run_source"}
    assert parsed.fingerprint() == replace(
        parsed,
        idempotency_key="idem-retry",
    ).fingerprint()
    for invalid in (
        {**data, "schema_version": "2"},
        {**data, "unknown": True},
        {**data, "max_scenes": 0},
        {**data, "max_scenes": 21},
        {**data, "min_similarity": 2},
    ):
        with pytest.raises(ReprocessingInputError):
            PipelineReprocessingSubmission.from_dict("run_source", invalid)


def test_reprocessing_plan_rejects_unknown_profile_and_mismatched_query_run():
    unknown_service, _, _ = service()
    with pytest.raises(ReprocessingInputError, match="unknown quality_profile"):
        asyncio.run(
            unknown_service.plan(
                PipelineReprocessingSubmission(
                    "idem-unknown",
                    "run_source",
                    "설명",
                    quality_profile="unknown-v1",
                )
            )
        )

    class WrongRunQueryService(QueryService):
        async def query(self, request):
            result = await super().query(request)
            return replace(result, run_id="run_other")

    wrong_service, _, _ = service(query=WrongRunQueryService())
    with pytest.raises(
        ReprocessingSourceNotReadyError,
        match="query result",
    ):
        asyncio.run(
            wrong_service.plan(
                PipelineReprocessingSubmission(
                    "idem-wrong-run",
                    "run_source",
                    "설명",
                )
            )
        )
