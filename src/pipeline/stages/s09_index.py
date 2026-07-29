"""9단계: 씬 카드를 SQLite FTS5 + 임베딩으로 인덱싱한다.

- 키워드 검색: FTS5 (unicode61 토크나이저)
- 의미 검색: sentence-transformers 다국어 임베딩 (정규화 float32 BLOB 저장)

입력: 08_timeline/timeline.json
출력:
- 09_index/index.db          : scene_cards / cards_fts / embeddings 테이블
- 09_index/index_summary.json : 인덱스 구성 확인용 요약
"""

import sqlite3
import time

import numpy as np

from ..context import PipelineContext
from ..logging_setup import stage_logger

NAME = "09_index"
OUTPUT = "09_index/index_summary.json"


def _card_text(card: dict) -> str:
    """검색 대상 텍스트: 캡션 + 전사문을 하나로 합친다."""
    parts = []
    if card["caption"]:
        parts.append(card["caption"])
    parts.extend(line["text"] for line in card["transcript"])
    return "\n".join(parts)


def run(ctx: PipelineContext) -> dict:
    log = stage_logger(NAME)
    out_dir = ctx.stage_dir(NAME)

    cards = ctx.load_json(
        ctx.out_root / "08_timeline" / "timeline.json"
    )["scene_cards"]
    texts = [_card_text(c) for c in cards]

    log.info("임베딩 모델 로드: %s", ctx.embed_model)
    t0 = time.monotonic()
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(ctx.embed_model)
    log.debug("모델 로드 완료 (%.1fs)", time.monotonic() - t0)

    t0 = time.monotonic()
    vectors = model.encode(texts, normalize_embeddings=True)
    dim = vectors.shape[1]
    log.info("씬 카드 %d개 임베딩 완료 (dim=%d, %.1fs)",
             len(texts), dim, time.monotonic() - t0)

    db_path = out_dir / "index.db"
    db_path.unlink(missing_ok=True)
    db = sqlite3.connect(db_path)
    db.executescript("""
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
    """)
    db.execute("INSERT INTO meta VALUES ('embed_model', ?)", (ctx.embed_model,))
    db.execute("INSERT INTO meta VALUES ('embed_dim', ?)", (str(dim),))

    for card, text, vec in zip(cards, texts, vectors):
        db.execute(
            "INSERT INTO scene_cards VALUES (?, ?, ?, ?, ?)",
            (card["scene_id"], card["start_sec"], card["end_sec"],
             card["caption"], text),
        )
        db.execute(
            "INSERT INTO cards_fts(rowid, card_text) VALUES (?, ?)",
            (card["scene_id"], text),
        )
        db.execute(
            "INSERT INTO embeddings VALUES (?, ?)",
            (card["scene_id"], np.asarray(vec, dtype=np.float32).tobytes()),
        )
        log.debug("씬 %02d 인덱싱: %d자", card["scene_id"], len(text))
    db.commit()
    db.close()
    log.info("인덱스 저장: %s (%.1fKB)",
             db_path.name, db_path.stat().st_size / 1024)

    summary = {
        "db": str(db_path.relative_to(ctx.out_root)),
        "embed_model": ctx.embed_model,
        "embed_dim": dim,
        "card_count": len(cards),
        "fts_tokenizer": "unicode61",
    }
    ctx.save_json(out_dir / "index_summary.json", summary)
    return {"card_count": len(cards), "embed_dim": dim}
