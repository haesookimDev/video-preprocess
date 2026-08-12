"""Deterministic retrieval quality evaluation over a fixed query set."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

from .query import PipelineQueryRequest, PipelineQueryResult


@dataclass(frozen=True, slots=True)
class RetrievalEvaluationCase:
    case_id: str
    query: str
    relevant_scene_ids: Sequence[int] = ()
    expect_no_answer: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.case_id, str) or not self.case_id.strip():
            raise ValueError("case_id must be a non-empty string")
        if not isinstance(self.query, str) or not self.query.strip():
            raise ValueError("query must be a non-empty string")
        scene_ids = tuple(self.relevant_scene_ids)
        if not all(
            isinstance(scene_id, int)
            and not isinstance(scene_id, bool)
            and scene_id >= 1
            for scene_id in scene_ids
        ):
            raise ValueError("relevant_scene_ids must be positive integers")
        if len(set(scene_ids)) != len(scene_ids):
            raise ValueError("relevant_scene_ids must be unique")
        if not isinstance(self.expect_no_answer, bool):
            raise TypeError("expect_no_answer must be a boolean")
        if self.expect_no_answer == bool(scene_ids):
            raise ValueError(
                "a case must define relevant scenes or expect no answer"
            )
        object.__setattr__(self, "case_id", self.case_id.strip())
        object.__setattr__(self, "query", self.query.strip())
        object.__setattr__(self, "relevant_scene_ids", scene_ids)

    @classmethod
    def from_dict(cls, value: object) -> "RetrievalEvaluationCase":
        if not isinstance(value, dict):
            raise ValueError("evaluation case must be an object")
        allowed = {
            "case_id",
            "query",
            "relevant_scene_ids",
            "expect_no_answer",
        }
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(
                "evaluation case contains unknown fields: "
                + ", ".join(sorted(unknown))
            )
        return cls(
            case_id=value.get("case_id"),
            query=value.get("query"),
            relevant_scene_ids=value.get("relevant_scene_ids", ()),
            expect_no_answer=value.get("expect_no_answer", False),
        )


@dataclass(frozen=True, slots=True)
class RetrievalCaseResult:
    case_id: str
    query: str
    expected_scene_ids: Sequence[int]
    returned_scene_ids: Sequence[int]
    expected_no_answer: bool
    predicted_no_answer: bool
    recall_at_k: float
    reciprocal_rank: float

    def to_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "query": self.query,
            "expected_scene_ids": list(self.expected_scene_ids),
            "returned_scene_ids": list(self.returned_scene_ids),
            "expected_no_answer": self.expected_no_answer,
            "predicted_no_answer": self.predicted_no_answer,
            "recall_at_k": self.recall_at_k,
            "reciprocal_rank": self.reciprocal_rank,
        }


@dataclass(frozen=True, slots=True)
class RetrievalEvaluationReport:
    case_count: int
    answer_case_count: int
    no_answer_case_count: int
    top_k: int
    min_similarity: float
    recall_at_k: float
    mean_reciprocal_rank: float
    no_answer_precision: float
    no_answer_recall: float
    cases: Sequence[RetrievalCaseResult]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "1",
            "case_count": self.case_count,
            "answer_case_count": self.answer_case_count,
            "no_answer_case_count": self.no_answer_case_count,
            "top_k": self.top_k,
            "min_similarity": self.min_similarity,
            "recall_at_k": self.recall_at_k,
            "mean_reciprocal_rank": self.mean_reciprocal_rank,
            "no_answer_precision": self.no_answer_precision,
            "no_answer_recall": self.no_answer_recall,
            "cases": [case.to_dict() for case in self.cases],
        }


class EvaluationQueryService(Protocol):
    async def query(self, request: PipelineQueryRequest) -> PipelineQueryResult: ...


def load_evaluation_cases(path: Path) -> tuple[RetrievalEvaluationCase, ...]:
    """Load and strictly validate a versioned evaluation dataset."""

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("evaluation dataset is unreadable") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "cases",
    }:
        raise ValueError("evaluation dataset has an invalid envelope")
    if payload["schema_version"] != "1" or not isinstance(
        payload["cases"], list
    ):
        raise ValueError("evaluation dataset schema is unsupported")
    cases = tuple(
        RetrievalEvaluationCase.from_dict(value) for value in payload["cases"]
    )
    if not 30 <= len(cases) <= 50:
        raise ValueError("evaluation dataset must contain 30 to 50 cases")
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("evaluation case_id values must be unique")
    return cases


async def evaluate_retrieval(
    service: EvaluationQueryService,
    cases: Sequence[RetrievalEvaluationCase],
    *,
    run_id: str,
    top_k: int = 3,
    min_similarity: float = 0.35,
) -> RetrievalEvaluationReport:
    """Measure answer ranking and no-answer classification."""

    if not callable(getattr(service, "query", None)):
        raise TypeError("service must implement query")
    normalized_cases = tuple(cases)
    if not normalized_cases:
        raise ValueError("cases must not be empty")
    results = []
    for case in normalized_cases:
        response = await service.query(
            PipelineQueryRequest(
                run_id=run_id,
                query=case.query,
                top_k=top_k,
                min_similarity=min_similarity,
            )
        )
        returned = tuple(match.scene_id for match in response.matches)
        relevant = set(case.relevant_scene_ids)
        recall = (
            0.0
            if not relevant
            else len(relevant.intersection(returned)) / len(relevant)
        )
        first_relevant_rank = next(
            (
                rank
                for rank, scene_id in enumerate(returned, start=1)
                if scene_id in relevant
            ),
            None,
        )
        reciprocal_rank = (
            0.0 if first_relevant_rank is None else 1.0 / first_relevant_rank
        )
        results.append(
            RetrievalCaseResult(
                case_id=case.case_id,
                query=case.query,
                expected_scene_ids=case.relevant_scene_ids,
                returned_scene_ids=returned,
                expected_no_answer=case.expect_no_answer,
                predicted_no_answer=response.no_answer,
                recall_at_k=recall,
                reciprocal_rank=reciprocal_rank,
            )
        )

    answer_results = [result for result in results if result.expected_scene_ids]
    no_answer_results = [
        result for result in results if result.expected_no_answer
    ]
    predicted_no_answer = [
        result for result in results if result.predicted_no_answer
    ]
    correct_no_answer = sum(
        result.expected_no_answer for result in predicted_no_answer
    )
    return RetrievalEvaluationReport(
        case_count=len(results),
        answer_case_count=len(answer_results),
        no_answer_case_count=len(no_answer_results),
        top_k=top_k,
        min_similarity=float(min_similarity),
        recall_at_k=_mean([result.recall_at_k for result in answer_results]),
        mean_reciprocal_rank=_mean(
            [result.reciprocal_rank for result in answer_results]
        ),
        no_answer_precision=(
            0.0
            if not predicted_no_answer
            else correct_no_answer / len(predicted_no_answer)
        ),
        no_answer_recall=(
            0.0
            if not no_answer_results
            else correct_no_answer / len(no_answer_results)
        ),
        cases=tuple(results),
    )


def _mean(values: Sequence[float]) -> float:
    return 0.0 if not values else sum(values) / len(values)
