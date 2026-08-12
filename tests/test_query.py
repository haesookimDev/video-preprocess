"""Unit tests for hybrid retrieval and context assembly."""

import json
import logging
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import query as query_module

from query import assemble_context, embed_search, fts_search, rrf_fuse
from video_preprocess.domain import EffectiveModel
from video_preprocess.inference import EmbeddingBatch


LOG = logging.getLogger("test.query")


def test_fts_search_returns_matching_scene() -> None:
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE VIRTUAL TABLE cards_fts USING fts5("
        "card_text, tokenize='unicode61')"
    )
    db.execute(
        "INSERT INTO cards_fts(rowid, card_text) VALUES (?, ?)",
        (1, "첫 번째 장면에서는 파이프라인을 설명합니다."),
    )
    db.execute(
        "INSERT INTO cards_fts(rowid, card_text) VALUES (?, ?)",
        (2, "두 번째 장면에서는 음성 구간 검출을 확인합니다."),
    )

    try:
        assert fts_search(db, "음성 검출", LOG) == [2]
    finally:
        db.close()


def test_rrf_fuse_combines_rankings() -> None:
    fused = rrf_fuse([[1, 2], [2, 3]], LOG)

    assert fused == [2, 1, 3]


def test_embed_search_uses_injected_service_and_existing_blob_schema() -> None:
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    db.execute("INSERT INTO meta VALUES ('embed_model', 'fake/model')")
    db.execute(
        "CREATE TABLE embeddings (scene_id INTEGER PRIMARY KEY, vector BLOB)"
    )
    db.executemany(
        "INSERT INTO embeddings VALUES (?, ?)",
        [
            (1, np.asarray([1.0, 0.0], dtype=np.float32).tobytes()),
            (2, np.asarray([0.0, 1.0], dtype=np.float32).tobytes()),
        ],
    )

    class FakeService:
        def embed(self, texts, **kwargs) -> EmbeddingBatch:
            assert texts == ["두 번째"]
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

    try:
        ranking = embed_search(db, "두 번째", LOG, FakeService())
    finally:
        db.close()

    assert ranking == [2, 1]


def test_assemble_context_places_best_scene_last(tmp_path: Path) -> None:
    timeline_dir = tmp_path / "09_timeline"
    timeline_dir.mkdir()
    timeline = {
        "scene_cards": [
            {
                "scene_id": 1,
                "start_sec": 0.0,
                "end_sec": 10.0,
                "caption": "first scene",
                "transcript": [
                    {
                        "start_sec": 1.0,
                        "end_sec": 2.0,
                        "speaker": None,
                        "text": "첫 번째 내용",
                    }
                ],
            },
            {
                "scene_id": 2,
                "start_sec": 10.0,
                "end_sec": 20.0,
                "caption": "second scene",
                "transcript": [
                    {
                        "start_sec": 11.0,
                        "end_sec": 12.0,
                        "speaker": "SPEAKER_00",
                        "text": "두 번째 내용",
                    }
                ],
            },
        ]
    }
    (timeline_dir / "timeline.json").write_text(
        json.dumps(timeline, ensure_ascii=False), encoding="utf-8"
    )

    context = assemble_context(tmp_path, top_ids=[2, 1])

    scene_2_position = context.index("### 씬 02")
    scene_1_position = context.index("### 씬 01")
    assert scene_1_position < scene_2_position
    assert "(SPEAKER_00) 두 번째 내용" in context


def test_main_creates_log_directory_for_external_index(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class Service:
        def __init__(self, *args, **kwargs):
            pass

        async def query(self, request):
            return SimpleNamespace(context="context")

    monkeypatch.setattr(query_module, "setup_logging", lambda _: LOG)
    monkeypatch.setattr(query_module, "QueryService", Service)
    monkeypatch.setattr(
        sys,
        "argv",
        ["query.py", str(tmp_path), "질의"],
    )

    assert query_module.main() == 0
    assert (tmp_path / "logs").is_dir()


def test_main_composes_remote_embedding_service_from_cli(
    tmp_path: Path,
    monkeypatch,
) -> None:
    seen = []

    class Service:
        def __init__(self, resolver, *, deployments, logger):
            seen.append(deployments)

        async def query(self, request):
            return SimpleNamespace(context="context")

    monkeypatch.setattr(query_module, "setup_logging", lambda _: LOG)
    monkeypatch.setattr(query_module, "QueryService", Service)
    monkeypatch.setenv("MODEL_SERVER_TOKEN", "private-token")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "query.py",
            str(tmp_path),
            "질의",
            "--embedding-endpoint",
            "https://models.example.test",
            "--embedding-token-env",
            "MODEL_SERVER_TOKEN",
        ],
    )

    assert query_module.main() == 0
    remote = seen[0].http_provider("embedding.default")
    assert remote.endpoint == "https://models.example.test"
    assert remote.auth_token == "private-token"
