"""Compatibility tests for the provider-backed embedding index Stage."""

import json
import logging
import sqlite3
from pathlib import Path

from pipeline.context import PipelineContext
from pipeline.stages import s10_index
from video_preprocess.domain import EffectiveModel
from video_preprocess.inference import EmbeddingBatch
from video_preprocess.services.query import fts_search


class FakeEmbeddingService:
    def __init__(self) -> None:
        self.texts = []

    def embed(self, texts, **kwargs) -> EmbeddingBatch:
        self.texts = list(texts)
        return EmbeddingBatch(
            vectors=((1.0, 0.0), (0.0, 1.0)),
            dimension=2,
            model=EffectiveModel(
                provider="fake.embedding",
                name="fake/model",
                revision="rev-1",
                runtime="fake/1.0",
            ),
            usage={"input_count": 2},
            timing={"inference_sec": 0.01},
        )


def test_index_stage_keeps_sqlite_schema_with_provider_metadata(
    tmp_path: Path,
) -> None:
    context = PipelineContext(
        video_path=tmp_path / "sample.mp4",
        out_root=tmp_path / "output" / "sample",
        embed_model="fake/model",
    )
    timeline_dir = context.stage_dir("09_timeline")
    timeline = {
        "scene_cards": [
            {
                "scene_id": 1,
                "start_sec": 0.0,
                "end_sec": 5.0,
                "caption": "첫 장면",
                "ocr_text": "OPENAI 화면",
                "chapter": {"title": "시작 챕터"},
                "subtitle_text": "내장 자막 내용",
                "audio_event_text": "music | applause",
                "transcript": [{"text": "첫 내용"}],
            },
            {
                "scene_id": 2,
                "start_sec": 5.0,
                "end_sec": 10.0,
                "caption": "둘째 장면",
                "transcript": [{"text": "둘째 내용"}],
            },
        ]
    }
    (timeline_dir / "timeline.json").write_text(
        json.dumps(timeline, ensure_ascii=False),
        encoding="utf-8",
    )
    service = FakeEmbeddingService()
    context.embedding_service = service

    result = s10_index.run(context)

    db = sqlite3.connect(context.out_root / "10_index" / "index.db")
    try:
        meta = dict(db.execute("SELECT key, value FROM meta"))
        embedding_count = db.execute(
            "SELECT count(*) FROM embeddings"
        ).fetchone()[0]
        keyword_ranking = fts_search(
            db,
            "첫　내용은?",
            logging.getLogger("test.index"),
        )
    finally:
        db.close()
    assert result == {"card_count": 2, "embed_dim": 2}
    assert service.texts == [
        "첫 장면\nOPENAI 화면\n시작 챕터\n내장 자막 내용\n"
        "music | applause\n첫 내용",
        "둘째 장면\n둘째 내용",
    ]
    assert meta["embed_model"] == "fake/model"
    assert meta["embed_provider"] == "fake.embedding"
    assert meta["embed_revision"] == "rev-1"
    assert meta["search_schema"] == "hybrid-search-v2"
    assert meta["text_normalization"].startswith("nfkc-")
    assert meta["keyword_index"] == "char-2-3gram-v1"
    assert embedding_count == 2
    assert keyword_ranking[0] == 1


def test_index_stage_requires_composed_embedding_service(
    tmp_path: Path,
) -> None:
    context = PipelineContext(
        video_path=tmp_path / "sample.mp4",
        out_root=tmp_path / "output" / "sample",
    )
    timeline_dir = context.stage_dir("09_timeline")
    (timeline_dir / "timeline.json").write_text(
        json.dumps({"scene_cards": []}),
        encoding="utf-8",
    )

    try:
        s10_index.run(context)
    except RuntimeError as exc:
        assert str(exc) == "embedding service is not configured"
    else:
        raise AssertionError("missing embedding service was accepted")
