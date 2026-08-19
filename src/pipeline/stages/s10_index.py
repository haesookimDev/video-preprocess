"""10단계: 씬 카드를 SQLite FTS5 + 임베딩으로 인덱싱한다.

- 키워드 검색: NFKC 정규화 + FTS5 단어/문자 2~3-gram
- 의미 검색: sentence-transformers 다국어 임베딩 (정규화 float32 BLOB 저장)

입력: 09_timeline/timeline.json
출력:
- 10_index/index.db          : scene_cards / cards_fts / embeddings 테이블
- 10_index/index_summary.json : 인덱스 구성 확인용 요약
"""

import sqlite3
import time

import numpy as np

from video_preprocess.retrieval import character_ngrams, normalize_search_text
from video_preprocess.retrieval.text import NGRAM_VERSION, NORMALIZATION_VERSION

from ..context import PipelineContext
from ..logging_setup import stage_logger

NAME = "10_index"
OUTPUT = "10_index/index_summary.json"


def _card_text(card: dict) -> str:
    """검색 대상 텍스트: 캡션 + OCR + 전사문을 하나로 합친다."""
    parts = []
    if card["caption"]:
        parts.append(card["caption"])
    if card.get("ocr_text"):
        parts.append(card["ocr_text"])
    parts.extend(line["text"] for line in card["transcript"])
    return "\n".join(parts)


def run(ctx: PipelineContext) -> dict:
    log = stage_logger(NAME)
    out_dir = ctx.stage_dir(NAME)

    cards = ctx.load_json(
        ctx.out_root / "09_timeline" / "timeline.json"
    )["scene_cards"]
    texts = [_card_text(c) for c in cards]

    log.info("임베딩 provider 준비: embedding.default → %s", ctx.embed_model)
    embedding_service = ctx.embedding_service
    if embedding_service is None:
        raise RuntimeError("embedding service is not configured")
    t0 = time.monotonic()
    batch = embedding_service.embed(
        texts,
        run_id=ctx.out_root.name,
        stage_run_id=NAME,
    )
    vectors = np.asarray(batch.vectors, dtype=np.float32)
    dim = batch.dimension
    log.info("씬 카드 %d개 임베딩 완료 (dim=%d, %.1fs)",
             len(texts), dim, time.monotonic() - t0)
    log.debug(
        "실제 임베딩 모델: provider=%s model=%s revision=%s runtime=%s",
        batch.model.provider,
        batch.model.name,
        batch.model.revision,
        batch.model.runtime,
    )

    db_path = out_dir / "index.db"
    db_path.unlink(missing_ok=True)
    db = sqlite3.connect(db_path)
    db.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE scene_cards (
            scene_id INTEGER PRIMARY KEY,
            start_sec REAL, end_sec REAL,
            caption TEXT, card_text TEXT,
            normalized_text TEXT, ngram_text TEXT
        );
        CREATE VIRTUAL TABLE cards_fts USING fts5(
            normalized_text, ngram_text,
            content='scene_cards', content_rowid='scene_id',
            tokenize='unicode61'
        );
        CREATE TABLE embeddings (
            scene_id INTEGER PRIMARY KEY, vector BLOB
        );
    """)
    db.execute("INSERT INTO meta VALUES ('embed_model', ?)", (ctx.embed_model,))
    db.execute("INSERT INTO meta VALUES ('embed_dim', ?)", (str(dim),))
    db.execute(
        "INSERT INTO meta VALUES ('embed_provider', ?)",
        (batch.model.provider,),
    )
    db.execute(
        "INSERT INTO meta VALUES ('embed_revision', ?)",
        (batch.model.revision,),
    )
    db.execute(
        "INSERT INTO meta VALUES ('embed_runtime', ?)",
        (batch.model.runtime or "",),
    )
    db.execute(
        "INSERT INTO meta VALUES ('search_schema', 'hybrid-search-v2')"
    )
    db.execute(
        "INSERT INTO meta VALUES ('text_normalization', ?)",
        (NORMALIZATION_VERSION,),
    )
    db.execute(
        "INSERT INTO meta VALUES ('keyword_index', ?)",
        (NGRAM_VERSION,),
    )

    for card, text, vec in zip(cards, texts, vectors):
        normalized_text = normalize_search_text(text)
        ngram_text = " ".join(character_ngrams(normalized_text))
        db.execute(
            "INSERT INTO scene_cards VALUES (?, ?, ?, ?, ?, ?, ?)",
            (card["scene_id"], card["start_sec"], card["end_sec"],
             card["caption"], text, normalized_text, ngram_text),
        )
        db.execute(
            "INSERT INTO cards_fts(rowid, normalized_text, ngram_text) "
            "VALUES (?, ?, ?)",
            (card["scene_id"], normalized_text, ngram_text),
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
        "embed_provider": batch.model.provider,
        "embed_revision": batch.model.revision,
        "embed_runtime": batch.model.runtime,
        "embed_dim": dim,
        "card_count": len(cards),
        "fts_tokenizer": "unicode61",
        "search_schema": "hybrid-search-v2",
        "text_normalization": NORMALIZATION_VERSION,
        "keyword_index": NGRAM_VERSION,
    }
    ctx.save_json(out_dir / "index_summary.json", summary)
    return {"card_count": len(cards), "embed_dim": dim}
