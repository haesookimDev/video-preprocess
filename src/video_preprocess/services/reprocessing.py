"""Query-guided two-pass reprocessing planning contracts."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from video_preprocess.domain import ArtifactRef
from video_preprocess.engine import DAGPlanner
from .query import PipelineQueryRequest, PipelineQueryResult, QueryMatch


SCHEMA_VERSION = "1"
VISUAL_DETAIL_PROFILE = "visual-detail-v1"


class ReprocessingServiceError(RuntimeError):
    """Base class for classified reprocessing planning failures."""


class ReprocessingInputError(ReprocessingServiceError):
    """A reprocessing request or source contract is invalid."""


class ReprocessingSourceNotReadyError(ReprocessingServiceError):
    """The source run lacks immutable artifacts required by the plan."""


class ReprocessingNoCandidatesError(ReprocessingServiceError):
    """The source query did not select any scene to reprocess."""


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReprocessingInputError(f"{field_name} must be a non-empty string")
    return value.strip()


def _unique_text_tuple(
    values: Sequence[str],
    field_name: str,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ReprocessingInputError(f"{field_name} must be an array")
    normalized = tuple(_require_text(value, field_name) for value in values)
    if len(normalized) != len(set(normalized)):
        raise ReprocessingInputError(f"{field_name} must not contain duplicates")
    return normalized


@dataclass(frozen=True, slots=True)
class PipelineReprocessingSubmission:
    """Transport-neutral mutation request for a derived pipeline run."""

    idempotency_key: str
    source_run_id: str
    query: str
    quality_profile: str = VISUAL_DETAIL_PROFILE
    max_scenes: int = 3
    min_similarity: float = 0.35
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ReprocessingInputError("unsupported schema_version")
        for field_name in (
            "idempotency_key",
            "source_run_id",
            "query",
            "quality_profile",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ReprocessingInputError(
                    f"{field_name} must be a non-empty string"
                )
            object.__setattr__(self, field_name, value.strip())
        if len(self.idempotency_key) > 200:
            raise ReprocessingInputError(
                "idempotency_key must be at most 200 characters"
            )
        if len(self.query) > 4000:
            raise ReprocessingInputError("query must be at most 4000 characters")
        if (
            isinstance(self.max_scenes, bool)
            or not isinstance(self.max_scenes, int)
            or not 1 <= self.max_scenes <= 20
        ):
            raise ReprocessingInputError(
                "max_scenes must be between 1 and 20"
            )
        if (
            isinstance(self.min_similarity, bool)
            or not isinstance(self.min_similarity, (int, float))
            or not math.isfinite(float(self.min_similarity))
            or not -1 <= float(self.min_similarity) <= 1
        ):
            raise ReprocessingInputError(
                "min_similarity must be between -1 and 1"
            )
        object.__setattr__(self, "min_similarity", float(self.min_similarity))

    def fingerprint(self) -> str:
        """Return the semantic request identity, excluding the retry key."""

        canonical = json.dumps(
            {
                "schema_version": self.schema_version,
                "source_run_id": self.source_run_id,
                "query": self.query,
                "quality_profile": self.quality_profile,
                "max_scenes": self.max_scenes,
                "min_similarity": self.min_similarity,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "idempotency_key": self.idempotency_key,
            "source_run_id": self.source_run_id,
            "query": self.query,
            "quality_profile": self.quality_profile,
            "max_scenes": self.max_scenes,
            "min_similarity": self.min_similarity,
        }

    @classmethod
    def from_dict(
        cls,
        source_run_id: str,
        data: Mapping[str, object],
    ) -> "PipelineReprocessingSubmission":
        if not isinstance(data, Mapping) or not all(
            isinstance(key, str) for key in data
        ):
            raise ReprocessingInputError(
                "reprocessing request must be an object"
            )
        required = {"schema_version", "idempotency_key", "query"}
        allowed = required | {
            "quality_profile",
            "max_scenes",
            "min_similarity",
        }
        missing = sorted(required - set(data))
        unknown = sorted(set(data) - allowed)
        if missing:
            raise ReprocessingInputError(
                "reprocessing request is missing: " + ", ".join(missing)
            )
        if unknown:
            raise ReprocessingInputError(
                "reprocessing request contains unknown fields: "
                + ", ".join(unknown)
            )
        return cls(
            schema_version=data["schema_version"],
            idempotency_key=data["idempotency_key"],
            source_run_id=source_run_id,
            query=data["query"],
            quality_profile=data.get(
                "quality_profile", VISUAL_DETAIL_PROFILE
            ),
            max_scenes=data.get("max_scenes", 3),
            min_similarity=data.get("min_similarity", 0.35),
        )


@dataclass(frozen=True, slots=True)
class ReprocessingQualityProfile:
    """Immutable server-owned quality and execution semantics."""

    name: str
    version: str
    from_stage: str
    stage_names: Sequence[str]
    selected_scene_stages: Sequence[str]
    full_materialization_stages: Sequence[str]
    overlay_artifacts: Sequence[str]
    query_artifacts: Sequence[str]
    settings_overrides: Mapping[str, object]
    boundary_source_artifacts: Mapping[str, str]
    selector_policy: str = "ranked-query-scenes-v1"
    overlay_policy: str = "copy-unselected-from-source-v1"
    output_policy: str = "derived-run-no-parent-overwrite-v1"
    cache_policy: str = "content-addressed-reprocessing-v1"

    def __post_init__(self) -> None:
        for field_name in (
            "name",
            "version",
            "from_stage",
            "selector_policy",
            "overlay_policy",
            "output_policy",
            "cache_policy",
        ):
            _require_text(getattr(self, field_name), field_name)
        stage_names = _unique_text_tuple(self.stage_names, "stage_names")
        selected = _unique_text_tuple(
            self.selected_scene_stages, "selected_scene_stages"
        )
        materialized = _unique_text_tuple(
            self.full_materialization_stages,
            "full_materialization_stages",
        )
        if set(selected) & set(materialized) or (
            set(selected) | set(materialized) != set(stage_names)
        ):
            raise ReprocessingInputError(
                "profile stage roles must partition stage_names"
            )
        object.__setattr__(self, "stage_names", stage_names)
        object.__setattr__(self, "selected_scene_stages", selected)
        object.__setattr__(
            self, "full_materialization_stages", materialized
        )
        object.__setattr__(
            self,
            "overlay_artifacts",
            _unique_text_tuple(self.overlay_artifacts, "overlay_artifacts"),
        )
        object.__setattr__(
            self,
            "query_artifacts",
            _unique_text_tuple(self.query_artifacts, "query_artifacts"),
        )
        if not isinstance(self.settings_overrides, Mapping):
            raise ReprocessingInputError(
                "settings_overrides must be an object"
            )
        object.__setattr__(
            self,
            "settings_overrides",
            dict(self.settings_overrides),
        )
        if not isinstance(self.boundary_source_artifacts, Mapping) or not all(
            isinstance(name, str)
            and name.strip()
            and isinstance(source, str)
            and source.strip()
            for name, source in self.boundary_source_artifacts.items()
        ):
            raise ReprocessingInputError(
                "boundary_source_artifacts must map non-empty names"
            )
        object.__setattr__(
            self,
            "boundary_source_artifacts",
            dict(self.boundary_source_artifacts),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "version": self.version,
            "selector_policy": self.selector_policy,
            "overlay_policy": self.overlay_policy,
            "output_policy": self.output_policy,
            "cache_policy": self.cache_policy,
            "settings_overrides": dict(self.settings_overrides),
            "boundary_source_artifacts": dict(
                sorted(self.boundary_source_artifacts.items())
            ),
        }


VISUAL_DETAIL_V1 = ReprocessingQualityProfile(
    name=VISUAL_DETAIL_PROFILE,
    version="1",
    from_stage="03_keyframes",
    stage_names=(
        "03_keyframes",
        "08_captions",
        "08_ocr",
        "09_timeline",
        "10_index",
        "11_context",
    ),
    selected_scene_stages=("03_keyframes", "08_captions", "08_ocr"),
    full_materialization_stages=("09_timeline", "10_index", "11_context"),
    overlay_artifacts=(
        "keyframes",
        "keyframe_images",
        "captions",
        "ocr",
    ),
    query_artifacts=("timeline", "search_index"),
    settings_overrides={
        "keyframes_per_scene": 3,
        "ocr_mode": "all",
    },
    boundary_source_artifacts={
        "source_keyframes": "keyframes",
        "source_keyframe_images": "keyframe_images",
        "source_captions": "captions",
        "source_ocr": "ocr",
    },
)


@dataclass(frozen=True, slots=True)
class ReprocessingSource:
    """Immutable first-pass artifacts resolved for one succeeded run."""

    run_id: str
    artifacts: Mapping[str, ArtifactRef]

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _require_text(self.run_id, "run_id"))
        if not isinstance(self.artifacts, Mapping):
            raise ReprocessingInputError("artifacts must be an object")
        normalized = dict(self.artifacts)
        if not all(
            isinstance(name, str)
            and name.strip()
            and isinstance(ref, ArtifactRef)
            for name, ref in normalized.items()
        ):
            raise ReprocessingInputError(
                "artifacts must map names to ArtifactRef values"
            )
        object.__setattr__(self, "artifacts", normalized)


class ReprocessingSourceResolver(Protocol):
    def resolve(self, run_id: str) -> ReprocessingSource: ...


class ReprocessingQueryService(Protocol):
    async def query(
        self, request: PipelineQueryRequest
    ) -> PipelineQueryResult: ...


@dataclass(frozen=True, slots=True)
class ReprocessingCandidate:
    """One ranked source scene selected for high-quality processing."""

    rank: int
    scene_id: int
    start_sec: float
    end_sec: float
    score: float
    reasons: Sequence[str]
    keyword_rank: int | None = None
    semantic_rank: int | None = None
    semantic_similarity: float | None = None

    def __post_init__(self) -> None:
        for field_name in ("rank", "scene_id"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ReprocessingInputError(f"{field_name} must be positive")
        for field_name in ("start_sec", "end_sec", "score"):
            value = getattr(self, field_name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ReprocessingInputError(f"{field_name} must be finite")
            object.__setattr__(self, field_name, float(value))
        if self.start_sec < 0 or self.end_sec < self.start_sec:
            raise ReprocessingInputError("candidate interval is invalid")
        reasons = _unique_text_tuple(self.reasons, "reasons")
        object.__setattr__(self, "reasons", reasons)

    @classmethod
    def from_match(cls, match: QueryMatch) -> "ReprocessingCandidate":
        if not isinstance(match, QueryMatch):
            raise TypeError("match must be a QueryMatch")
        return cls(
            rank=match.rank,
            scene_id=match.scene_id,
            start_sec=match.start_sec,
            end_sec=match.end_sec,
            score=match.score,
            reasons=tuple(match.reasons),
            keyword_rank=match.keyword_rank,
            semantic_rank=match.semantic_rank,
            semantic_similarity=match.semantic_similarity,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "rank": self.rank,
            "scene_id": self.scene_id,
            "start_sec": self.start_sec,
            "end_sec": self.end_sec,
            "score": self.score,
            "reasons": list(self.reasons),
            "keyword_rank": self.keyword_rank,
            "semantic_rank": self.semantic_rank,
            "semantic_similarity": self.semantic_similarity,
        }


@dataclass(frozen=True, slots=True)
class ReprocessingStageContract:
    """Stage version and selection role captured by one plan."""

    name: str
    version: str
    scope: str

    def __post_init__(self) -> None:
        for field_name in ("name", "version", "scope"):
            object.__setattr__(
                self,
                field_name,
                _require_text(getattr(self, field_name), field_name),
            )
        if self.scope not in {"selected-scenes", "full-materialization"}:
            raise ReprocessingInputError("stage scope is invalid")

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "version": self.version,
            "scope": self.scope,
        }


@dataclass(frozen=True, slots=True)
class ReprocessingPlan:
    """Auditable, execution-free plan for a future derived run."""

    plan_id: str
    request_fingerprint: str
    plan_fingerprint: str
    source_run_id: str
    query: str
    normalized_query: str
    profile: ReprocessingQualityProfile
    candidates: Sequence[ReprocessingCandidate]
    stages: Sequence[ReprocessingStageContract]
    boundary_inputs: Sequence[str]
    source_artifacts: Mapping[str, ArtifactRef]
    pending_capabilities: Sequence[str]
    execution_ready: bool = False
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ReprocessingInputError("unsupported plan schema_version")
        for field_name in (
            "plan_id",
            "request_fingerprint",
            "plan_fingerprint",
            "source_run_id",
            "query",
            "normalized_query",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_text(getattr(self, field_name), field_name),
            )
        if not isinstance(self.profile, ReprocessingQualityProfile):
            raise ReprocessingInputError("profile must be a quality profile")
        candidates = tuple(self.candidates)
        if not candidates or not all(
            isinstance(value, ReprocessingCandidate) for value in candidates
        ):
            raise ReprocessingInputError("candidates must not be empty")
        scene_ids = tuple(value.scene_id for value in candidates)
        if len(scene_ids) != len(set(scene_ids)):
            raise ReprocessingInputError("candidate scenes must be unique")
        stages = tuple(self.stages)
        if not stages or not all(
            isinstance(value, ReprocessingStageContract) for value in stages
        ):
            raise ReprocessingInputError("stages must not be empty")
        object.__setattr__(self, "candidates", candidates)
        object.__setattr__(self, "stages", stages)
        object.__setattr__(
            self,
            "boundary_inputs",
            _unique_text_tuple(self.boundary_inputs, "boundary_inputs"),
        )
        object.__setattr__(
            self,
            "pending_capabilities",
            _unique_text_tuple(
                self.pending_capabilities,
                "pending_capabilities",
            ),
        )
        if not isinstance(self.source_artifacts, Mapping) or not all(
            isinstance(name, str)
            and name.strip()
            and isinstance(ref, ArtifactRef)
            for name, ref in self.source_artifacts.items()
        ):
            raise ReprocessingInputError(
                "source_artifacts must map names to ArtifactRef values"
            )
        object.__setattr__(self, "source_artifacts", dict(self.source_artifacts))
        if not isinstance(self.execution_ready, bool):
            raise ReprocessingInputError("execution_ready must be a boolean")

    @property
    def selected_scene_ids(self) -> tuple[int, ...]:
        return tuple(candidate.scene_id for candidate in self.candidates)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "request_fingerprint": self.request_fingerprint,
            "plan_fingerprint": self.plan_fingerprint,
            "source_run_id": self.source_run_id,
            "query": {
                "text": self.query,
                "normalized_text": self.normalized_query,
            },
            "quality_profile": self.profile.to_dict(),
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "execution": {
                "ready": self.execution_ready,
                "selected_scene_ids": list(self.selected_scene_ids),
                "stages": [stage.to_dict() for stage in self.stages],
                "boundary_inputs": list(self.boundary_inputs),
                "force_stages": [stage.name for stage in self.stages],
                "pending_capabilities": list(self.pending_capabilities),
            },
            "source_artifacts": {
                name: ref.to_dict()
                for name, ref in sorted(self.source_artifacts.items())
            },
        }


class QueryReprocessingApplicationService:
    """Turn read-only query results into an immutable derived-run plan."""

    _PENDING_CAPABILITIES = (
        "derived-run-application-runtime-v1",
    )

    def __init__(
        self,
        planner: DAGPlanner,
        query_service: ReprocessingQueryService,
        source_resolver: ReprocessingSourceResolver,
        *,
        profiles: Mapping[str, ReprocessingQualityProfile] | None = None,
    ) -> None:
        if not isinstance(planner, DAGPlanner):
            raise TypeError("planner must be a DAGPlanner")
        if not callable(getattr(query_service, "query", None)):
            raise TypeError("query_service must implement query")
        if not callable(getattr(source_resolver, "resolve", None)):
            raise TypeError("source_resolver must implement resolve")
        configured = dict(profiles or {VISUAL_DETAIL_PROFILE: VISUAL_DETAIL_V1})
        if not configured or not all(
            isinstance(name, str)
            and isinstance(profile, ReprocessingQualityProfile)
            and name == profile.name
            for name, profile in configured.items()
        ):
            raise TypeError("profiles must map names to quality profiles")
        self.planner = planner
        self.query_service = query_service
        self.source_resolver = source_resolver
        self.profiles = configured

    async def plan(
        self,
        submission: PipelineReprocessingSubmission,
    ) -> ReprocessingPlan:
        if not isinstance(submission, PipelineReprocessingSubmission):
            raise TypeError(
                "submission must be a PipelineReprocessingSubmission"
            )
        try:
            profile = self.profiles[submission.quality_profile]
        except KeyError as exc:
            raise ReprocessingInputError(
                "unknown quality_profile: " + submission.quality_profile
            ) from exc
        execution = self.planner.plan(from_stage=profile.from_stage)
        if execution.stage_names != tuple(profile.stage_names):
            raise ReprocessingInputError(
                "quality profile does not match the current Stage DAG"
            )
        source = self.source_resolver.resolve(submission.source_run_id)
        if source.run_id != submission.source_run_id:
            raise ReprocessingSourceNotReadyError(
                "resolved source run does not match the request"
            )
        required_artifacts = tuple(sorted(
            {
                profile.boundary_source_artifacts.get(name, name)
                for name in execution.boundary_inputs
            }
            | set(profile.overlay_artifacts)
            | set(profile.query_artifacts)
        ))
        missing = tuple(
            name for name in required_artifacts if name not in source.artifacts
        )
        if missing:
            raise ReprocessingSourceNotReadyError(
                "source run is missing provenance artifacts: "
                + ", ".join(missing)
            )

        query_result = await self.query_service.query(
            PipelineQueryRequest(
                run_id=source.run_id,
                query=submission.query,
                top_k=submission.max_scenes,
                min_similarity=submission.min_similarity,
                max_context_tokens=128,
                adjacent_scenes=0,
            )
        )
        if query_result.run_id != source.run_id:
            raise ReprocessingSourceNotReadyError(
                "query result does not match the source run"
            )
        if query_result.query != submission.query:
            raise ReprocessingInputError(
                "query result does not match the reprocessing request"
            )
        candidates = (
            ()
            if query_result.no_answer
            else self._select_candidates(
                query_result,
                max_scenes=submission.max_scenes,
            )
        )
        if not candidates:
            raise ReprocessingNoCandidatesError(
                "query did not select a reprocessing candidate"
            )
        stages = tuple(
            ReprocessingStageContract(
                name=stage.name,
                version=stage.stage_version,
                scope=(
                    "selected-scenes"
                    if stage.name in profile.selected_scene_stages
                    else "full-materialization"
                ),
            )
            for stage in execution.stages
        )
        selected_artifacts = {
            name: source.artifacts[name] for name in required_artifacts
        }
        plan_fingerprint = self._plan_fingerprint(
            source_run_id=source.run_id,
            normalized_query=query_result.normalized_query,
            profile=profile,
            candidates=candidates,
            stages=stages,
            source_artifacts=selected_artifacts,
        )
        return ReprocessingPlan(
            plan_id=f"reprocess_plan_{plan_fingerprint[:24]}",
            request_fingerprint=submission.fingerprint(),
            plan_fingerprint=plan_fingerprint,
            source_run_id=source.run_id,
            query=submission.query,
            normalized_query=query_result.normalized_query,
            profile=profile,
            candidates=candidates,
            stages=stages,
            boundary_inputs=execution.boundary_inputs,
            source_artifacts=selected_artifacts,
            pending_capabilities=self._PENDING_CAPABILITIES,
        )

    @staticmethod
    def _select_candidates(
        result: PipelineQueryResult,
        *,
        max_scenes: int,
    ) -> tuple[ReprocessingCandidate, ...]:
        if not isinstance(result, PipelineQueryResult):
            raise TypeError("query service returned an invalid result")
        selected = []
        seen = set()
        for match in result.matches:
            if match.scene_id in seen:
                continue
            selected.append(ReprocessingCandidate.from_match(match))
            seen.add(match.scene_id)
            if len(selected) == max_scenes:
                break
        return tuple(selected)

    @staticmethod
    def _plan_fingerprint(
        *,
        source_run_id: str,
        normalized_query: str,
        profile: ReprocessingQualityProfile,
        candidates: Sequence[ReprocessingCandidate],
        stages: Sequence[ReprocessingStageContract],
        source_artifacts: Mapping[str, ArtifactRef],
    ) -> str:
        payload = {
            "contract": "query-guided-reprocessing-plan-v1",
            "source_run_id": source_run_id,
            "normalized_query": normalized_query,
            "quality_profile": profile.to_dict(),
            "selected_scene_ids": [item.scene_id for item in candidates],
            "stages": [stage.to_dict() for stage in stages],
            "source_artifacts": {
                name: ref.to_dict()
                for name, ref in sorted(source_artifacts.items())
            },
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
