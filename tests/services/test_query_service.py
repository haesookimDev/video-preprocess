"""Tests for the shared hybrid query application service."""

import asyncio
import json
import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from video_preprocess.domain import EffectiveModel
from video_preprocess.inference import EmbeddingBatch
from video_preprocess.services import (
    FixedQueryTargetResolver,
    LocalPipelineRunQueryResolver,
    PipelineQueryRequest,
    QueryIndexNotFoundError,
    QueryService,
    QueryServiceInputError,
    QueryRunNotReadyError,
    PublicRunStatus,
)


LOG = logging.getLogger("test.query-service")


class FakeEmbeddingService:
    async def embed_async(self, texts, **options):
        assert texts == ["두 번째"]
        return self._batch()

    def embed(self, texts, **options):
        return self._batch()

    @staticmethod
    def _batch():
        return EmbeddingBatch(
            vectors=((0.0, 1.0),),
            dimension=2,
            model=EffectiveModel(
                provider="fake",
                name="fake/model",
                revision="rev-1",
            ),
            usage={},
            timing={},
        )


class CharacterTokenCounter:
    model_name = "fake/tokenizer"

    @staticmethod
    def count(text):
        return len(text)

    @staticmethod
    def truncate(text, max_tokens):
        return text[:max_tokens]


def token_counter_factory(model_name, revision):
    return CharacterTokenCounter()


def write_query_fixture(root: Path) -> None:
    timeline_dir = root / "09_timeline"
    index_dir = root / "10_index"
    timeline_dir.mkdir(parents=True)
    index_dir.mkdir(parents=True)
    cards = [
        {
            "scene_id": 1,
            "start_sec": 0.0,
            "end_sec": 10.0,
            "caption": "first scene",
            "transcript": [{"start_sec": 1.0, "text": "첫 번째"}],
        },
        {
            "scene_id": 2,
            "start_sec": 10.0,
            "end_sec": 20.0,
            "caption": "second scene",
            "ocr_text": "OPENAI dashboard",
            "chapter": {"title": "Dashboard chapter"},
            "subtitle_text": "Embedded welcome",
            "transcript": [
                {
                    "start_sec": 11.0,
                    "speaker": "SPEAKER_00",
                    "text": "두 번째",
                }
            ],
        },
    ]
    (timeline_dir / "timeline.json").write_text(
        json.dumps({"scene_cards": cards}, ensure_ascii=False),
        encoding="utf-8",
    )
    with sqlite3.connect(index_dir / "index.db") as db:
        db.executescript(
            """
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE scene_cards (
                scene_id INTEGER PRIMARY KEY,
                start_sec REAL, end_sec REAL,
                caption TEXT, card_text TEXT
            );
            CREATE VIRTUAL TABLE cards_fts USING fts5(
                card_text, content='scene_cards', content_rowid='scene_id',
                tokenize='unicode61'
            );
            CREATE TABLE embeddings (
                scene_id INTEGER PRIMARY KEY, vector BLOB
            );
            """
        )
        db.executemany(
            "INSERT INTO meta VALUES (?, ?)",
            (("embed_model", "fake/model"), ("embed_revision", "rev-1")),
        )
        for card, vector in zip(cards, ((1.0, 0.0), (0.0, 1.0))):
            text = card["transcript"][0]["text"]
            db.execute(
                "INSERT INTO scene_cards VALUES (?, ?, ?, ?, ?)",
                (
                    card["scene_id"],
                    card["start_sec"],
                    card["end_sec"],
                    card["caption"],
                    text,
                ),
            )
            db.execute(
                "INSERT INTO cards_fts(rowid, card_text) VALUES (?, ?)",
                (card["scene_id"], text),
            )
            db.execute(
                "INSERT INTO embeddings VALUES (?, ?)",
                (
                    card["scene_id"],
                    np.asarray(vector, dtype=np.float32).tobytes(),
                ),
            )


def test_query_service_returns_ranked_matches_and_context(tmp_path: Path) -> None:
    write_query_fixture(tmp_path)
    seen = []

    def factory(model_name, revision):
        seen.append((model_name, revision))
        return FakeEmbeddingService()

    service = QueryService(
        FixedQueryTargetResolver(tmp_path),
        embedding_factory=factory,
        token_counter_factory=token_counter_factory,
        logger=LOG,
    )

    result = asyncio.run(
        service.query(PipelineQueryRequest("run-test", "두 번째", top_k=2))
    )

    assert seen == [("fake/model", "rev-1")]
    assert result.matches[0].scene_id == 2
    assert result.matches[0].rank == 1
    assert result.matches[0].reasons == ("keyword", "semantic")
    assert result.matches[0].keyword_rank == 1
    assert result.matches[0].semantic_similarity == 1.0
    assert result.normalized_query == "두 번째"
    assert result.no_answer is False
    assert result.context_stats["token_count"] <= 4096
    assert "### 씬 02" in result.context
    assert "화면 텍스트: OPENAI dashboard" in result.context
    assert "챕터: Dashboard chapter" in result.context
    assert "내장 자막: Embedded welcome" in result.context
    assert result.to_dict()["matches"][0]["scene_id"] == 2
    assert str(tmp_path) not in str(result.to_dict())


def test_query_service_rejects_semantic_only_results_below_threshold(
    tmp_path: Path,
) -> None:
    write_query_fixture(tmp_path)

    class LowSimilarityService(FakeEmbeddingService):
        async def embed_async(self, texts, **options):
            assert texts == ["존재하지 않는 주제"]
            return self._batch()

        @staticmethod
        def _batch():
            return EmbeddingBatch(
                vectors=((0.1, 0.1),),
                dimension=2,
                model=EffectiveModel(
                    provider="fake",
                    name="fake/model",
                    revision="rev-1",
                ),
                usage={},
                timing={},
            )

    service = QueryService(
        FixedQueryTargetResolver(tmp_path),
        embedding_factory=lambda *_: LowSimilarityService(),
        token_counter_factory=token_counter_factory,
        logger=LOG,
    )

    result = asyncio.run(
        service.query(
            PipelineQueryRequest(
                "run-test",
                "존재하지 않는 주제",
                min_similarity=0.5,
            )
        )
    )

    assert result.no_answer is True
    assert result.matches == ()
    assert "질의 관련 씬 카드" in result.context


def test_query_service_reuses_embedding_service_within_process(
    tmp_path: Path,
) -> None:
    write_query_fixture(tmp_path)
    factory_calls = []

    def factory(model_name, revision):
        factory_calls.append((model_name, revision))
        return FakeEmbeddingService()

    service = QueryService(
        FixedQueryTargetResolver(tmp_path),
        embedding_factory=factory,
        token_counter_factory=token_counter_factory,
        logger=LOG,
    )
    request = PipelineQueryRequest("run-test", "두 번째")

    asyncio.run(service.query(request))
    asyncio.run(service.query(request))

    assert factory_calls == [("fake/model", "rev-1")]


def test_query_service_classifies_missing_or_invalid_index(
    tmp_path: Path,
) -> None:
    service = QueryService(FixedQueryTargetResolver(tmp_path), logger=LOG)

    with pytest.raises(QueryIndexNotFoundError):
        asyncio.run(
            service.query(PipelineQueryRequest("run-test", "질의"))
        )
    with pytest.raises(QueryServiceInputError):
        PipelineQueryRequest.from_dict(
            "run-test",
            {"schema_version": "1", "query": "질의", "unknown": True},
        )


def test_api_target_resolver_requires_succeeded_run_and_bounds_path(
    tmp_path: Path,
) -> None:
    class Runs:
        status = PublicRunStatus.RUNNING

        def get(self, run_id):
            return SimpleNamespace(status=self.status)

    runs = Runs()
    resolver = LocalPipelineRunQueryResolver(runs, tmp_path)

    with pytest.raises(QueryRunNotReadyError):
        resolver.resolve("run-test")
    runs.status = PublicRunStatus.SUCCEEDED
    assert resolver.resolve("run-test") == tmp_path / "run-test"
