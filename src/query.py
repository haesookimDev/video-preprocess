"""중요 구간 검색 + LLM 입력 컨텍스트 조립 CLI.

10_index/index.db 에 대해 하이브리드 검색(FTS5 키워드 + 임베딩 의미)을 수행하고,
RRF로 순위를 융합해 top-k 씬 카드로 컨텍스트 블록을 조립해 출력한다.
LLM 호출은 하지 않는다 (조립까지가 프로토타입 범위).

사용법:
    python src/query.py output/<video_stem> "질의 문장" [--topk 3]
"""

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

from pipeline.logging_setup import setup_logging
from video_preprocess.inference import EmbeddingService
from video_preprocess.inference.local import get_local_embedding_service

RRF_K = 60  # Reciprocal Rank Fusion 상수


def _fmt_ts(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    return f"{m:02d}:{s:02d}"


def fts_search(db: sqlite3.Connection, query: str, log) -> list[int]:
    """FTS5 키워드 검색 → scene_id 순위 목록 (bm25 오름차순)."""
    # 질의문을 OR 토큰 질의로 변환 (조사가 붙은 한국어 어절 대응)
    tokens = [t for t in query.split() if t]
    match_expr = " OR ".join(f'"{t}"' for t in tokens)
    try:
        rows = db.execute(
            "SELECT rowid, bm25(cards_fts) FROM cards_fts "
            "WHERE cards_fts MATCH ? ORDER BY bm25(cards_fts)",
            (match_expr,),
        ).fetchall()
    except sqlite3.OperationalError as e:
        log.warning("FTS 질의 실패 (%s) — 키워드 검색 생략", e)
        return []
    for rank, (sid, score) in enumerate(rows, start=1):
        log.debug("FTS %d위: 씬 %02d (bm25=%.3f)", rank, sid, score)
    return [sid for sid, _ in rows]


def _meta_value(
    db: sqlite3.Connection,
    key: str,
) -> str | None:
    row = db.execute(
        "SELECT value FROM meta WHERE key=?",
        (key,),
    ).fetchone()
    return None if row is None else row[0]


def embed_search(
    db: sqlite3.Connection,
    query: str,
    log,
    embedding_service: EmbeddingService | None = None,
) -> list[int]:
    """임베딩 코사인 유사도 검색 → scene_id 순위 목록."""
    model_name = _meta_value(db, "embed_model")
    if model_name is None:
        raise ValueError("index meta does not contain embed_model")
    revision = _meta_value(db, "embed_revision")
    requested_revision = None if revision in {None, "default"} else revision
    service = embedding_service or get_local_embedding_service(
        model_name,
        revision=requested_revision,
    )
    log.debug("임베딩 provider 호출: %s", model_name)
    batch = service.embed(
        [query],
        run_id="query",
        stage_run_id="query_embedding",
    )
    qvec = np.asarray(batch.vectors[0], dtype=np.float32)

    rows = db.execute("SELECT scene_id, vector FROM embeddings").fetchall()
    scored = [
        (sid, float(np.dot(qvec, np.frombuffer(blob, dtype=np.float32))))
        for sid, blob in rows
    ]
    scored.sort(key=lambda x: -x[1])
    for rank, (sid, sim) in enumerate(scored, start=1):
        log.debug("임베딩 %d위: 씬 %02d (cos=%.4f)", rank, sid, sim)
    return [sid for sid, _ in scored]


def rrf_fuse(rankings: list[list[int]], log) -> list[int]:
    """Reciprocal Rank Fusion으로 여러 순위 목록을 융합한다."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, sid in enumerate(ranking, start=1):
            scores[sid] = scores.get(sid, 0.0) + 1.0 / (RRF_K + rank)
    fused = sorted(scores, key=lambda sid: -scores[sid])
    for sid in fused:
        log.debug("RRF: 씬 %02d = %.5f", sid, scores[sid])
    return fused


def assemble_context(out_root: Path, top_ids: list[int]) -> str:
    """전체 요약층(씬 목차) + 선별된 씬 카드 원문으로 컨텍스트를 조립한다."""
    import json

    timeline = json.loads(
        (out_root / "09_timeline" / "timeline.json").read_text(encoding="utf-8")
    )["scene_cards"]
    by_id = {c["scene_id"]: c for c in timeline}

    lines = ["## 영상 개요 (전체 씬 목차)"]
    for c in timeline:
        head = c["caption"] or (
            c["transcript"][0]["text"][:30] if c["transcript"] else "(내용 없음)"
        )
        lines.append(
            f"- 씬 {c['scene_id']:02d} "
            f"[{_fmt_ts(c['start_sec'])}~{_fmt_ts(c['end_sec'])}] {head}"
        )

    lines.append("")
    lines.append("## 질의 관련 씬 카드 (관련도 순, 마지막이 최상위)")
    # lost-in-the-middle 완화: 최상위 씬을 질문 직전(맨 뒤)에 배치
    for sid in reversed(top_ids):
        c = by_id[sid]
        lines.append(
            f"\n### 씬 {sid:02d} [{_fmt_ts(c['start_sec'])}~{_fmt_ts(c['end_sec'])}]"
        )
        if c["caption"]:
            lines.append(f"시각: {c['caption']}")
        for t in c["transcript"]:
            who = f" ({t['speaker']})" if t.get("speaker") else ""
            lines.append(f"[{_fmt_ts(t['start_sec'])}]{who} {t['text']}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="씬 검색 + 컨텍스트 조립")
    parser.add_argument("out_root", type=Path,
                        help="파이프라인 출력 디렉토리 (예: output/sample)")
    parser.add_argument("query", help="질의 문장")
    parser.add_argument("--topk", type=int, default=3)
    args = parser.parse_args()

    out_root = args.out_root.resolve()
    db_path = out_root / "10_index" / "index.db"
    if not db_path.exists():
        print(f"오류: 인덱스가 없습니다: {db_path} (파이프라인을 먼저 실행)",
              file=sys.stderr)
        return 1

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = out_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = setup_logging(log_dir / f"query_{run_id}.log")
    log.info("질의: %s (topk=%d)", args.query, args.topk)

    db = sqlite3.connect(db_path)
    fts_ranking = fts_search(db, args.query, log)
    embed_ranking = embed_search(db, args.query, log)
    log.info("FTS 히트 %d개, 임베딩 후보 %d개",
             len(fts_ranking), len(embed_ranking))

    fused = rrf_fuse([fts_ranking, embed_ranking], log)
    top_ids = fused[: args.topk]
    log.info("최종 top-%d 씬: %s", args.topk, top_ids)
    db.close()

    context = assemble_context(out_root, top_ids)
    print("\n" + "=" * 60)
    print("조립된 LLM 입력 컨텍스트")
    print("=" * 60)
    print(context)
    return 0


if __name__ == "__main__":
    sys.exit(main())
