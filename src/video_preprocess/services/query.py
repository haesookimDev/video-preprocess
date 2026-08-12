"""Shared hybrid-search and context-assembly application service."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from video_preprocess.inference import (
    EmbeddingService,
    InferenceDeploymentSettings,
    create_configured_embedding_service,
)
from video_preprocess.retrieval import normalize_search_text, search_terms
from video_preprocess.tokenization import (
    HuggingFaceTokenCounter,
    TokenCounter,
    sentence_transformer_tokenizer_model,
)

from .pipeline_runs import PipelineRunService, PublicRunStatus


RRF_K = 60
SCHEMA_VERSION = "1"


class QueryServiceError(RuntimeError):
    """Base class for classified query use-case failures."""


class QueryServiceInputError(QueryServiceError):
    """A query request or index contract is invalid."""


class QueryIndexNotFoundError(QueryServiceError):
    """The target run does not have a searchable index."""


class QueryRunNotReadyError(QueryServiceError):
    """The target run is not in a queryable terminal state."""


@dataclass(frozen=True, slots=True)
class PipelineQueryRequest:
    """Transport-independent search request for one completed run."""

    run_id: str
    query: str
    top_k: int = 5
    min_similarity: float = 0.35
    max_context_tokens: int = 4096
    adjacent_scenes: int = 1
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise QueryServiceInputError("unsupported schema_version")
        for field_name in ("run_id", "query"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise QueryServiceInputError(
                    f"{field_name} must be a non-empty string"
                )
            object.__setattr__(self, field_name, value.strip())
        if len(self.query) > 4000:
            raise QueryServiceInputError("query must be at most 4000 characters")
        if (
            isinstance(self.top_k, bool)
            or not isinstance(self.top_k, int)
            or not 1 <= self.top_k <= 100
        ):
            raise QueryServiceInputError("top_k must be between 1 and 100")
        if (
            isinstance(self.min_similarity, bool)
            or not isinstance(self.min_similarity, (int, float))
            or not -1.0 <= self.min_similarity <= 1.0
        ):
            raise QueryServiceInputError(
                "min_similarity must be between -1 and 1"
            )
        object.__setattr__(self, "min_similarity", float(self.min_similarity))
        if (
            isinstance(self.max_context_tokens, bool)
            or not isinstance(self.max_context_tokens, int)
            or self.max_context_tokens < 128
        ):
            raise QueryServiceInputError(
                "max_context_tokens must be at least 128"
            )
        if (
            isinstance(self.adjacent_scenes, bool)
            or not isinstance(self.adjacent_scenes, int)
            or not 0 <= self.adjacent_scenes <= 5
        ):
            raise QueryServiceInputError(
                "adjacent_scenes must be between 0 and 5"
            )

    @classmethod
    def from_dict(
        cls,
        run_id: str,
        data: Mapping[str, object],
    ) -> "PipelineQueryRequest":
        if not isinstance(data, Mapping) or not all(
            isinstance(key, str) for key in data
        ):
            raise QueryServiceInputError("query request must be an object")
        required = {"schema_version", "query"}
        allowed = required | {
            "top_k",
            "min_similarity",
            "max_context_tokens",
            "adjacent_scenes",
        }
        missing = sorted(required - set(data))
        unknown = sorted(set(data) - allowed)
        if missing:
            raise QueryServiceInputError(
                "query request is missing: " + ", ".join(missing)
            )
        if unknown:
            raise QueryServiceInputError(
                "query request contains unknown fields: "
                + ", ".join(unknown)
            )
        return cls(
            run_id=run_id,
            query=data["query"],
            top_k=data.get("top_k", 5),
            min_similarity=data.get("min_similarity", 0.35),
            max_context_tokens=data.get("max_context_tokens", 4096),
            adjacent_scenes=data.get("adjacent_scenes", 1),
            schema_version=data["schema_version"],
        )


@dataclass(frozen=True, slots=True)
class QueryMatch:
    """One ranked scene returned by the query use case."""

    rank: int
    scene_id: int
    start_sec: float
    end_sec: float
    score: float
    text: str
    keyword_rank: int | None = None
    keyword_score: float | None = None
    semantic_rank: int | None = None
    semantic_similarity: float | None = None
    reasons: Sequence[str] = ()

    def __post_init__(self) -> None:
        normalized = tuple(self.reasons)
        if not all(isinstance(reason, str) and reason for reason in normalized):
            raise TypeError("reasons must contain non-empty strings")
        object.__setattr__(self, "reasons", normalized)

    def to_dict(self) -> dict[str, object]:
        return {
            "rank": self.rank,
            "scene_id": self.scene_id,
            "start_sec": self.start_sec,
            "end_sec": self.end_sec,
            "score": self.score,
            "text": self.text,
            "keyword_rank": self.keyword_rank,
            "keyword_score": self.keyword_score,
            "semantic_rank": self.semantic_rank,
            "semantic_similarity": self.semantic_similarity,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class PipelineQueryResult:
    """Ranked scenes and context ready for an LLM caller."""

    run_id: str
    query: str
    context: str
    matches: Sequence[QueryMatch]
    context_stats: Mapping[str, object]
    normalized_query: str = ""
    no_answer: bool = False
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported query result schema_version")
        normalized = tuple(self.matches)
        if not all(isinstance(match, QueryMatch) for match in normalized):
            raise TypeError("matches must contain QueryMatch values")
        object.__setattr__(self, "matches", normalized)
        normalized_query = self.normalized_query or normalize_search_text(
            self.query
        )
        object.__setattr__(self, "normalized_query", normalized_query)
        if not isinstance(self.no_answer, bool):
            raise TypeError("no_answer must be a boolean")
        object.__setattr__(self, "no_answer", self.no_answer or not normalized)
        if not isinstance(self.context_stats, Mapping):
            raise TypeError("context_stats must be a mapping")
        stats = dict(self.context_stats)
        object.__setattr__(self, "context_stats", stats)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "query": self.query,
            "normalized_query": self.normalized_query,
            "no_answer": self.no_answer,
            "context": self.context,
            "context_stats": dict(self.context_stats),
            "matches": [match.to_dict() for match in self.matches],
        }


class QueryTargetResolver(Protocol):
    def resolve(self, run_id: str) -> Path: ...


class FixedQueryTargetResolver:
    """Resolve every CLI query to one explicitly selected output tree."""

    def __init__(self, output_root: Path) -> None:
        self.output_root = Path(output_root).resolve()

    def resolve(self, run_id: str) -> Path:
        return self.output_root


class LocalPipelineRunQueryResolver:
    """Map a succeeded API run ID to its private local workspace."""

    def __init__(
        self,
        run_service: PipelineRunService,
        workspace_root: Path,
    ) -> None:
        if not callable(getattr(run_service, "get", None)):
            raise TypeError("run_service must implement get")
        self.run_service = run_service
        self.workspace_root = Path(workspace_root).resolve()

    def resolve(self, run_id: str) -> Path:
        snapshot = self.run_service.get(run_id)
        if snapshot.status is not PublicRunStatus.SUCCEEDED:
            raise QueryRunNotReadyError(
                "pipeline run must succeed before it can be queried"
            )
        target = (self.workspace_root / run_id).resolve()
        try:
            target.relative_to(self.workspace_root)
        except ValueError as exc:
            raise QueryServiceInputError("run workspace escapes root") from exc
        return target


EmbeddingFactory = Callable[[str, str | None], EmbeddingService]
TokenCounterFactory = Callable[[str, str | None], TokenCounter]


@dataclass(frozen=True, slots=True)
class KeywordHit:
    scene_id: int
    rank: int
    score: float


@dataclass(frozen=True, slots=True)
class SemanticHit:
    scene_id: int
    rank: int
    similarity: float


@dataclass(frozen=True, slots=True)
class ContextAssembly:
    text: str
    stats: Mapping[str, object]


class QueryService:
    """Search one index and assemble context through shared inference."""

    def __init__(
        self,
        target_resolver: QueryTargetResolver,
        *,
        deployments: InferenceDeploymentSettings | None = None,
        embedding_factory: EmbeddingFactory | None = None,
        context_tokenizer_model: str | None = None,
        token_counter_factory: TokenCounterFactory | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        if not callable(getattr(target_resolver, "resolve", None)):
            raise TypeError("target_resolver must implement resolve")
        self.target_resolver = target_resolver
        self.deployments = deployments or InferenceDeploymentSettings()
        if not isinstance(self.deployments, InferenceDeploymentSettings):
            raise TypeError("deployments must be InferenceDeploymentSettings")
        self.embedding_factory = embedding_factory or self._embedding_service
        if not callable(self.embedding_factory):
            raise TypeError("embedding_factory must be callable")
        self._embedding_service_cache: dict[
            tuple[str, str | None], EmbeddingService
        ] = {}
        if context_tokenizer_model is not None and (
            not isinstance(context_tokenizer_model, str)
            or not context_tokenizer_model.strip()
        ):
            raise ValueError("context_tokenizer_model must be non-empty or None")
        self.context_tokenizer_model = (
            None
            if context_tokenizer_model is None
            else context_tokenizer_model.strip()
        )
        self.token_counter_factory = (
            token_counter_factory or self._token_counter
        )
        self._token_counter_cache: dict[
            tuple[str, str | None], TokenCounter
        ] = {}
        self.log = logger or logging.getLogger("video_preprocess.query")

    async def query(self, request: PipelineQueryRequest) -> PipelineQueryResult:
        if not isinstance(request, PipelineQueryRequest):
            raise TypeError("request must be PipelineQueryRequest")
        output_root = Path(self.target_resolver.resolve(request.run_id)).resolve()
        db_path = output_root / "10_index" / "index.db"
        timeline_path = output_root / "09_timeline" / "timeline.json"
        if not db_path.is_file() or not timeline_path.is_file():
            raise QueryIndexNotFoundError(
                "completed run does not contain index and timeline artifacts"
            )
        self.log.info("질의: %s (topk=%d)", request.query, request.top_k)
        normalized_query = normalize_search_text(request.query)
        if not normalized_query:
            raise QueryServiceInputError(
                "query must contain at least one letter or number"
            )
        try:
            with sqlite3.connect(db_path) as db:
                model_name = _meta_value(db, "embed_model")
                if model_name is None:
                    raise QueryServiceInputError(
                        "index meta does not contain embed_model"
                    )
                revision = _meta_value(db, "embed_revision")
                requested_revision = (
                    None if revision in {None, "default"} else revision
                )
                embedding_service = self._cached_embedding_service(
                    model_name,
                    requested_revision,
                )
                keyword_hits = fts_search_with_scores(
                    db,
                    normalized_query,
                    self.log,
                )
                semantic_hits = await _embed_search_with_scores_async(
                    db,
                    normalized_query,
                    self.log,
                    embedding_service,
                    run_id=request.run_id,
                )
                accepted_semantic = [
                    hit
                    for hit in semantic_hits
                    if hit.similarity >= request.min_similarity
                ]
                fused = rrf_fuse_with_scores(
                    [
                        [hit.scene_id for hit in keyword_hits],
                        [hit.scene_id for hit in accepted_semantic],
                    ],
                    self.log,
                )
                selected = fused[: request.top_k]
                rows = _scene_rows(db, [scene_id for scene_id, _ in selected])
        except sqlite3.DatabaseError as exc:
            raise QueryServiceInputError("search index is invalid") from exc
        top_ids = [scene_id for scene_id, _ in selected if scene_id in rows]
        tokenizer_model = self.context_tokenizer_model or (
            sentence_transformer_tokenizer_model(model_name)
        )
        tokenizer_revision = (
            requested_revision if self.context_tokenizer_model is None else None
        )
        token_counter = self._cached_token_counter(
            tokenizer_model,
            tokenizer_revision,
        )
        assembly = assemble_context_with_budget(
            output_root,
            top_ids,
            token_counter=token_counter,
            max_tokens=request.max_context_tokens,
            adjacent_scenes=request.adjacent_scenes,
        )
        context = assembly.text
        keyword_by_id = {hit.scene_id: hit for hit in keyword_hits}
        semantic_by_id = {hit.scene_id: hit for hit in semantic_hits}
        matches = []
        for rank, (scene_id, score) in enumerate(selected, start=1):
            if scene_id not in rows:
                continue
            keyword = keyword_by_id.get(scene_id)
            semantic = semantic_by_id.get(scene_id)
            reasons = []
            if keyword is not None:
                reasons.append("keyword")
            if (
                semantic is not None
                and semantic.similarity >= request.min_similarity
            ):
                reasons.append("semantic")
            matches.append(
                QueryMatch(
                    rank=rank,
                    scene_id=scene_id,
                    start_sec=float(rows[scene_id][0]),
                    end_sec=float(rows[scene_id][1]),
                    score=score,
                    text=str(rows[scene_id][2]),
                    keyword_rank=None if keyword is None else keyword.rank,
                    keyword_score=None if keyword is None else keyword.score,
                    semantic_rank=None if semantic is None else semantic.rank,
                    semantic_similarity=(
                        None if semantic is None else semantic.similarity
                    ),
                    reasons=reasons,
                )
            )
        self.log.info("최종 top-%d 씬: %s", request.top_k, top_ids)
        return PipelineQueryResult(
            run_id=request.run_id,
            query=request.query,
            context=context,
            matches=matches,
            normalized_query=normalized_query,
            no_answer=not matches,
            context_stats=assembly.stats,
        )

    def _embedding_service(
        self,
        model_name: str,
        revision: str | None,
    ) -> EmbeddingService:
        return create_configured_embedding_service(
            model_name,
            deployments=self.deployments,
            revision=revision,
        )

    def _cached_embedding_service(
        self,
        model_name: str,
        revision: str | None,
    ) -> EmbeddingService:
        key = (model_name, revision)
        service = self._embedding_service_cache.get(key)
        if service is None:
            service = self.embedding_factory(model_name, revision)
            self._embedding_service_cache[key] = service
        return service

    @staticmethod
    def _token_counter(
        model_name: str,
        revision: str | None,
    ) -> TokenCounter:
        return HuggingFaceTokenCounter(model_name, revision=revision)

    def _cached_token_counter(
        self,
        model_name: str,
        revision: str | None,
    ) -> TokenCounter:
        key = (model_name, revision)
        counter = self._token_counter_cache.get(key)
        if counter is None:
            counter = self.token_counter_factory(model_name, revision)
            self._token_counter_cache[key] = counter
        return counter


def _fmt_ts(sec: float) -> str:
    minutes, seconds = divmod(int(sec), 60)
    return f"{minutes:02d}:{seconds:02d}"


def fts_search(db: sqlite3.Connection, query: str, log) -> list[int]:
    """Return FTS5 scene IDs ordered by ascending bm25."""

    return [hit.scene_id for hit in fts_search_with_scores(db, query, log)]


def fts_search_with_scores(
    db: sqlite3.Connection,
    query: str,
    log,
) -> list[KeywordHit]:
    """Return ranked keyword hits from v2 n-grams or a legacy FTS table."""

    words, ngrams = search_terms(query)
    if not words:
        return []
    columns = {
        str(row[1]) for row in db.execute("PRAGMA table_info(cards_fts)")
    }
    modern = {"normalized_text", "ngram_text"}.issubset(columns)
    if modern:
        terms = [f'normalized_text:"{word}"' for word in words]
        terms.extend(f'ngram_text:"{gram}"' for gram in ngrams[:128])
        match_expr = " OR ".join(terms)
        score_expression = "bm25(cards_fts, 5.0, 1.0)"
    else:
        match_expr = " OR ".join(f'"{word}"' for word in words)
        score_expression = "bm25(cards_fts)"
    try:
        rows = db.execute(
            f"SELECT rowid, {score_expression} FROM cards_fts "
            f"WHERE cards_fts MATCH ? ORDER BY {score_expression}, rowid",
            (match_expr,),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        log.warning("FTS 질의 실패 (%s) — 키워드 검색 생략", exc)
        return []
    hits = [
        KeywordHit(
            scene_id=int(scene_id),
            rank=rank,
            score=-float(bm25_score),
        )
        for rank, (scene_id, bm25_score) in enumerate(rows, start=1)
    ]
    for hit in hits:
        log.debug(
            "키워드 %d위: 씬 %02d (score=%.4f)",
            hit.rank,
            hit.scene_id,
            hit.score,
        )
    return hits


def _meta_value(db: sqlite3.Connection, key: str) -> str | None:
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return None if row is None else row[0]


def embed_search(
    db: sqlite3.Connection,
    query: str,
    log,
    embedding_service: EmbeddingService,
) -> list[int]:
    """Compatibility helper for synchronous query unit tests."""

    batch = embedding_service.embed(
        [query], run_id="query", stage_run_id="query_embedding"
    )
    return [
        hit.scene_id
        for hit in _rank_embeddings_with_scores(db, batch.vectors[0], log)
    ]


async def _embed_search_async(
    db: sqlite3.Connection,
    query: str,
    log,
    embedding_service: EmbeddingService,
    *,
    run_id: str,
) -> list[int]:
    batch = await embedding_service.embed_async(
        [query], run_id=run_id, stage_run_id="query_embedding"
    )
    return [
        hit.scene_id
        for hit in _rank_embeddings_with_scores(db, batch.vectors[0], log)
    ]


async def _embed_search_with_scores_async(
    db: sqlite3.Connection,
    query: str,
    log,
    embedding_service: EmbeddingService,
    *,
    run_id: str,
) -> list[SemanticHit]:
    batch = await embedding_service.embed_async(
        [query], run_id=run_id, stage_run_id="query_embedding"
    )
    return _rank_embeddings_with_scores(db, batch.vectors[0], log)


def _rank_embeddings(
    db: sqlite3.Connection,
    query_vector: Sequence[float],
    log,
) -> list[int]:
    """Compatibility helper returning semantic IDs without scores."""

    return [
        hit.scene_id
        for hit in _rank_embeddings_with_scores(db, query_vector, log)
    ]


def _rank_embeddings_with_scores(
    db: sqlite3.Connection,
    query_vector: Sequence[float],
    log,
) -> list[SemanticHit]:
    qvec = np.asarray(query_vector, dtype=np.float32)
    if qvec.ndim != 1 or not np.all(np.isfinite(qvec)):
        raise QueryServiceInputError("query embedding is invalid")
    rows = db.execute("SELECT scene_id, vector FROM embeddings").fetchall()
    scored = []
    for scene_id, blob in rows:
        vector = np.frombuffer(blob, dtype=np.float32)
        if vector.shape != qvec.shape or not np.all(np.isfinite(vector)):
            raise QueryServiceInputError(
                "stored embedding dimension or values are invalid"
            )
        scored.append((int(scene_id), float(np.dot(qvec, vector))))
    scored.sort(key=lambda item: (-item[1], item[0]))
    hits = [
        SemanticHit(scene_id=scene_id, rank=rank, similarity=similarity)
        for rank, (scene_id, similarity) in enumerate(scored, start=1)
    ]
    for hit in hits:
        log.debug(
            "임베딩 %d위: 씬 %02d (cos=%.4f)",
            hit.rank,
            hit.scene_id,
            hit.similarity,
        )
    return hits


def rrf_fuse_with_scores(
    rankings: Sequence[Sequence[int]],
    log,
) -> list[tuple[int, float]]:
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, scene_id in enumerate(ranking, start=1):
            scores[scene_id] = scores.get(scene_id, 0.0) + 1.0 / (
                RRF_K + rank
            )
    fused = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    for scene_id, score in fused:
        log.debug("RRF: 씬 %02d = %.5f", scene_id, score)
    return fused


def rrf_fuse(rankings: list[list[int]], log) -> list[int]:
    """Compatibility helper returning IDs without public scores."""

    return [scene_id for scene_id, _ in rrf_fuse_with_scores(rankings, log)]


def _scene_rows(
    db: sqlite3.Connection,
    scene_ids: Sequence[int],
) -> dict[int, tuple[float, float, str]]:
    if not scene_ids:
        return {}
    placeholders = ",".join("?" for _ in scene_ids)
    rows = db.execute(
        "SELECT scene_id, start_sec, end_sec, card_text FROM scene_cards "
        f"WHERE scene_id IN ({placeholders})",
        tuple(scene_ids),
    ).fetchall()
    return {
        int(scene_id): (float(start), float(end), str(text))
        for scene_id, start, end, text in rows
    }


def assemble_context(output_root: Path, top_ids: Sequence[int]) -> str:
    """Assemble the overview plus selected cards, best match last."""

    timeline = _load_timeline(output_root)
    return _render_legacy_context(timeline, top_ids)


def assemble_context_with_budget(
    output_root: Path,
    top_ids: Sequence[int],
    *,
    token_counter: TokenCounter,
    max_tokens: int,
    adjacent_scenes: int = 1,
) -> ContextAssembly:
    """Select ranked cards and neighbors without exceeding a token budget."""

    if not callable(getattr(token_counter, "count", None)) or not callable(
        getattr(token_counter, "truncate", None)
    ):
        raise TypeError("token_counter must implement count and truncate")
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
        raise ValueError("max_tokens must be a positive integer")
    timeline = _load_timeline(output_root)
    by_id = {int(card["scene_id"]): card for card in timeline}
    missing = [scene_id for scene_id in top_ids if scene_id not in by_id]
    if missing:
        raise QueryServiceInputError(
            "search index references unknown timeline scenes"
        )
    candidates = _expanded_scene_candidates(
        timeline,
        top_ids,
        adjacent_scenes=adjacent_scenes,
    )
    header = "## 질의 관련 씬 카드"
    if not candidates:
        text = header + "\n(관련 결과 없음)"
        text = _fit_whole_text(text, token_counter, max_tokens)
        return ContextAssembly(
            text=text,
            stats={
                "tokenizer_model": token_counter.model_name,
                "max_tokens": max_tokens,
                "token_count": token_counter.count(text),
                "requested_scene_ids": list(top_ids),
                "expanded_scene_ids": [],
                "included_scene_ids": [],
                "excluded_scene_ids": [],
                "truncated_scene_ids": [],
            },
        )

    included: list[tuple[int, str]] = []
    excluded = []
    truncated = []
    for scene_id in candidates:
        full = _render_scene_card(by_id[scene_id])
        proposed = _render_budgeted_context(header, included + [(scene_id, full)])
        if token_counter.count(proposed) <= max_tokens:
            included.append((scene_id, full))
            continue
        compact = _compact_scene_card(by_id[scene_id])
        fitted = _fit_card(
            header,
            included,
            scene_id,
            compact,
            token_counter,
            max_tokens,
        )
        if fitted is None:
            excluded.append(scene_id)
            continue
        included.append((scene_id, fitted))
        truncated.append(scene_id)

    text = _render_budgeted_context(header, included)
    return ContextAssembly(
        text=text,
        stats={
            "tokenizer_model": token_counter.model_name,
            "max_tokens": max_tokens,
            "token_count": token_counter.count(text),
            "requested_scene_ids": list(top_ids),
            "expanded_scene_ids": list(candidates),
            "included_scene_ids": [scene_id for scene_id, _ in included],
            "excluded_scene_ids": excluded,
            "truncated_scene_ids": truncated,
        },
    )


def _load_timeline(output_root: Path) -> list[dict]:
    try:
        payload = json.loads(
            (output_root / "09_timeline" / "timeline.json").read_text(
                encoding="utf-8"
            )
        )
        timeline = payload["scene_cards"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise QueryServiceInputError("timeline artifact is invalid") from exc
    if not isinstance(timeline, list):
        raise QueryServiceInputError("timeline artifact is invalid")
    return timeline


def _render_legacy_context(
    timeline: Sequence[dict],
    top_ids: Sequence[int],
) -> str:
    by_id = {int(card["scene_id"]): card for card in timeline}
    missing = [scene_id for scene_id in top_ids if scene_id not in by_id]
    if missing:
        raise QueryServiceInputError(
            "search index references unknown timeline scenes"
        )
    lines = ["## 영상 개요 (전체 씬 목차)"]
    for card in timeline:
        head = card["caption"] or (
            card["transcript"][0]["text"][:30]
            if card["transcript"]
            else "(내용 없음)"
        )
        lines.append(
            f"- 씬 {card['scene_id']:02d} "
            f"[{_fmt_ts(card['start_sec'])}~{_fmt_ts(card['end_sec'])}] "
            f"{head}"
        )
    lines.extend(("", "## 질의 관련 씬 카드 (관련도 순, 마지막이 최상위)"))
    for scene_id in reversed(top_ids):
        lines.append("\n" + _render_scene_card(by_id[scene_id]))
    return "\n".join(lines)


def _expanded_scene_candidates(
    timeline: Sequence[dict],
    top_ids: Sequence[int],
    *,
    adjacent_scenes: int,
) -> tuple[int, ...]:
    positions = {
        int(card["scene_id"]): index for index, card in enumerate(timeline)
    }
    candidates = []
    seen = set()
    for scene_id in top_ids:
        center = positions[scene_id]
        ordered_positions = [center]
        for distance in range(1, adjacent_scenes + 1):
            ordered_positions.extend((center - distance, center + distance))
        for position in ordered_positions:
            if not 0 <= position < len(timeline):
                continue
            candidate = int(timeline[position]["scene_id"])
            if candidate not in seen:
                seen.add(candidate)
                candidates.append(candidate)
    return tuple(candidates)


def _render_scene_card(card: Mapping[str, object]) -> str:
    lines = [
        f"### 씬 {int(card['scene_id']):02d} "
        f"[{_fmt_ts(float(card['start_sec']))}~"
        f"{_fmt_ts(float(card['end_sec']))}]"
    ]
    if card.get("caption"):
        lines.append(f"시각: {card['caption']}")
    for transcript in card["transcript"]:
        speaker = (
            f" ({transcript['speaker']})"
            if transcript.get("speaker")
            else ""
        )
        lines.append(
            f"[{_fmt_ts(transcript['start_sec'])}]"
            f"{speaker} {transcript['text']}"
        )
    if not card["transcript"]:
        lines.append("(발화 없음)")
    return "\n".join(lines)


def _compact_scene_card(card: Mapping[str, object]) -> str:
    heading = _render_scene_card({**card, "caption": None, "transcript": []}).splitlines()[0]
    body = card.get("caption") or (
        card["transcript"][0]["text"] if card["transcript"] else "(내용 없음)"
    )
    return f"{heading}\n{body}"


def _render_budgeted_context(
    header: str,
    included: Sequence[tuple[int, str]],
) -> str:
    if not included:
        return header
    return header + "\n\n" + "\n\n".join(
        block for _, block in reversed(included)
    )


def _fit_card(
    header: str,
    included: Sequence[tuple[int, str]],
    scene_id: int,
    block: str,
    counter: TokenCounter,
    max_tokens: int,
) -> str | None:
    base = _render_budgeted_context(header, included)
    available = max_tokens - counter.count(base) - 2
    while available > 0:
        fitted = counter.truncate(block, available)
        if not fitted:
            return None
        proposed = _render_budgeted_context(
            header,
            list(included) + [(scene_id, fitted)],
        )
        if counter.count(proposed) <= max_tokens:
            return fitted
        available -= 1
    return None


def _fit_whole_text(
    text: str,
    counter: TokenCounter,
    max_tokens: int,
) -> str:
    if counter.count(text) <= max_tokens:
        return text
    return counter.truncate(text, max_tokens)
