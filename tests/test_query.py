"""Unit tests for hybrid retrieval and context assembly."""

import json
import logging
import sqlite3
from pathlib import Path

from query import assemble_context, fts_search, rrf_fuse


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
