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
        allowed = required | {"top_k"}
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

    def to_dict(self) -> dict[str, object]:
        return {
            "rank": self.rank,
            "scene_id": self.scene_id,
            "start_sec": self.start_sec,
            "end_sec": self.end_sec,
            "score": self.score,
            "text": self.text,
        }


@dataclass(frozen=True, slots=True)
class PipelineQueryResult:
    """Ranked scenes and context ready for an LLM caller."""

    run_id: str
    query: str
    context: str
    matches: Sequence[QueryMatch]
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported query result schema_version")
        normalized = tuple(self.matches)
        if not all(isinstance(match, QueryMatch) for match in normalized):
            raise TypeError("matches must contain QueryMatch values")
        object.__setattr__(self, "matches", normalized)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "query": self.query,
            "context": self.context,
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


class QueryService:
    """Search one index and assemble context through shared inference."""

    def __init__(
        self,
        target_resolver: QueryTargetResolver,
        *,
        deployments: InferenceDeploymentSettings | None = None,
        embedding_factory: EmbeddingFactory | None = None,
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
                embedding_service = self.embedding_factory(
                    model_name,
                    requested_revision,
                )
                fts_ranking = fts_search(db, request.query, self.log)
                embed_ranking = await _embed_search_async(
                    db,
                    request.query,
                    self.log,
                    embedding_service,
                    run_id=request.run_id,
                )
                fused = rrf_fuse_with_scores(
                    [fts_ranking, embed_ranking], self.log
                )
                selected = fused[: request.top_k]
                rows = _scene_rows(db, [scene_id for scene_id, _ in selected])
        except sqlite3.DatabaseError as exc:
            raise QueryServiceInputError("search index is invalid") from exc
        top_ids = [scene_id for scene_id, _ in selected if scene_id in rows]
        context = assemble_context(output_root, top_ids)
        matches = tuple(
            QueryMatch(
                rank=rank,
                scene_id=scene_id,
                start_sec=float(rows[scene_id][0]),
                end_sec=float(rows[scene_id][1]),
                score=score,
                text=str(rows[scene_id][2]),
            )
            for rank, (scene_id, score) in enumerate(selected, start=1)
            if scene_id in rows
        )
        self.log.info("최종 top-%d 씬: %s", request.top_k, top_ids)
        return PipelineQueryResult(
            run_id=request.run_id,
            query=request.query,
            context=context,
            matches=matches,
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


def _fmt_ts(sec: float) -> str:
    minutes, seconds = divmod(int(sec), 60)
    return f"{minutes:02d}:{seconds:02d}"


def fts_search(db: sqlite3.Connection, query: str, log) -> list[int]:
    """Return FTS5 scene IDs ordered by ascending bm25."""

    tokens = [token for token in query.split() if token]
    if not tokens:
        return []
    match_expr = " OR ".join(f'"{token}"' for token in tokens)
    try:
        rows = db.execute(
            "SELECT rowid, bm25(cards_fts) FROM cards_fts "
            "WHERE cards_fts MATCH ? ORDER BY bm25(cards_fts)",
            (match_expr,),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        log.warning("FTS 질의 실패 (%s) — 키워드 검색 생략", exc)
        return []
    return [int(scene_id) for scene_id, _ in rows]


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
    return _rank_embeddings(db, batch.vectors[0], log)


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
    return _rank_embeddings(db, batch.vectors[0], log)


def _rank_embeddings(
    db: sqlite3.Connection,
    query_vector: Sequence[float],
    log,
) -> list[int]:
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
    for rank, (scene_id, similarity) in enumerate(scored, start=1):
        log.debug(
            "임베딩 %d위: 씬 %02d (cos=%.4f)",
            rank,
            scene_id,
            similarity,
        )
    return [scene_id for scene_id, _ in scored]


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

    try:
        payload = json.loads(
            (output_root / "09_timeline" / "timeline.json").read_text(
                encoding="utf-8"
            )
        )
        timeline = payload["scene_cards"]
        by_id = {int(card["scene_id"]): card for card in timeline}
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise QueryServiceInputError("timeline artifact is invalid") from exc
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
        card = by_id[scene_id]
        lines.append(
            f"\n### 씬 {scene_id:02d} "
            f"[{_fmt_ts(card['start_sec'])}~{_fmt_ts(card['end_sec'])}]"
        )
        if card["caption"]:
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
    return "\n".join(lines)
