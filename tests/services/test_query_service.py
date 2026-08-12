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
        logger=LOG,
    )

    result = asyncio.run(
        service.query(PipelineQueryRequest("run-test", "두 번째", top_k=2))
    )

    assert seen == [("fake/model", "rev-1")]
    assert result.matches[0].scene_id == 2
    assert result.matches[0].rank == 1
    assert result.matches[0].score > result.matches[1].score
    assert "### 씬 02" in result.context
    assert result.to_dict()["matches"][0]["scene_id"] == 2
    assert str(tmp_path) not in str(result.to_dict())


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
