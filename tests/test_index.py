"""Compatibility tests for the provider-backed embedding index Stage."""

import json
import sqlite3
from pathlib import Path

from pipeline.context import PipelineContext
from pipeline.stages import s10_index
from video_preprocess.domain import EffectiveModel
from video_preprocess.inference import EmbeddingBatch


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
    monkeypatch,
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
    monkeypatch.setattr(
        s10_index,
        "get_local_embedding_service",
        lambda _: service,
    )

    result = s10_index.run(context)

    db = sqlite3.connect(context.out_root / "10_index" / "index.db")
    try:
        meta = dict(db.execute("SELECT key, value FROM meta"))
        embedding_count = db.execute(
            "SELECT count(*) FROM embeddings"
        ).fetchone()[0]
    finally:
        db.close()
    assert result == {"card_count": 2, "embed_dim": 2}
    assert service.texts == ["첫 장면\n첫 내용", "둘째 장면\n둘째 내용"]
    assert meta["embed_model"] == "fake/model"
    assert meta["embed_provider"] == "fake.embedding"
    assert meta["embed_revision"] == "rev-1"
    assert embedding_count == 2

