"""Tests for fixed-dataset retrieval metrics."""

import asyncio
from pathlib import Path

import pytest

from video_preprocess.services import (
    PipelineQueryResult,
    QueryMatch,
    RetrievalEvaluationCase,
    evaluate_retrieval,
    load_evaluation_cases,
)


DATASET = (
    Path(__file__).parents[1]
    / "fixtures"
    / "retrieval_v1"
    / "sample_queries.json"
)


def _match(rank: int, scene_id: int) -> QueryMatch:
    return QueryMatch(
        rank=rank,
        scene_id=scene_id,
        start_sec=0.0,
        end_sec=1.0,
        score=1.0 / rank,
        text=f"scene {scene_id}",
        reasons=("semantic",),
    )


def test_loads_versioned_36_case_sample_dataset() -> None:
    cases = load_evaluation_cases(DATASET)

    assert len(cases) == 36
    assert sum(case.expect_no_answer for case in cases) == 12
    assert {scene_id for case in cases for scene_id in case.relevant_scene_ids} == {
        1,
        2,
        3,
    }


def test_evaluation_calculates_ranking_and_no_answer_metrics() -> None:
    cases = (
        RetrievalEvaluationCase("answer-1", "one", (1,)),
        RetrievalEvaluationCase("answer-2", "two", (2,)),
        RetrievalEvaluationCase("no-answer-1", "none", expect_no_answer=True),
        RetrievalEvaluationCase("no-answer-2", "false", expect_no_answer=True),
    )

    class Service:
        async def query(self, request):
            matches = {
                "one": (_match(1, 1),),
                "two": (_match(1, 3), _match(2, 2)),
                "none": (),
                "false": (_match(1, 3),),
            }[request.query]
            return PipelineQueryResult(
                run_id=request.run_id,
                query=request.query,
                context="context",
                matches=matches,
                context_stats={
                    "tokenizer_model": "fake",
                    "max_tokens": 4096,
                    "token_count": 1,
                    "requested_scene_ids": [],
                    "expanded_scene_ids": [],
                    "included_scene_ids": [],
                    "excluded_scene_ids": [],
                    "truncated_scene_ids": [],
                },
            )

    report = asyncio.run(
        evaluate_retrieval(Service(), cases, run_id="evaluation")
    )

    assert report.recall_at_k == 1.0
    assert report.mean_reciprocal_rank == 0.75
    assert report.no_answer_precision == 1.0
    assert report.no_answer_recall == 0.5
    assert len(report.to_dict()["cases"]) == 4


def test_dataset_rejects_out_of_range_case_count(tmp_path: Path) -> None:
    path = tmp_path / "small.json"
    path.write_text(
        '{"schema_version":"1","cases":[]}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="30 to 50"):
        load_evaluation_cases(path)
