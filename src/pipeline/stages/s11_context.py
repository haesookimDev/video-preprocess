"""11단계(최종): LLM 입력용 컨텍스트 최종본을 조립한다.

전처리 산출물 전체를 하나의 자기완결적 컨텍스트로 묶는다. LLM 호출은 하지
않으며, 이 파일을 그대로 로컬 LLM의 컨텍스트로 넣어 요약·질의응답·이벤트
분석에 사용하는 것이 목표다.

구성: 포맷 안내(전문) → 영상 메타데이터 → 씬 목차 → 씬 카드 전문

입력: 01_probe, 07_diarize, 09_timeline 산출물
출력:
- 11_context/context.md   : LLM 컨텍스트 최종본 (그대로 프롬프트에 삽입)
- 11_context/context.json : 동일 내용의 구조화 버전 (프로그래밍 방식 조립용)
"""

from ..context import PipelineContext
from ..logging_setup import stage_logger

NAME = "11_context"
OUTPUT = "11_context/context.md"

PREAMBLE = """\
아래는 영상을 전처리하여 만든 타임라인 컨텍스트다. 형식 규칙:
- 시간은 [분:초] 형식이며 영상 시작 기준이다.
- `시각:`은 씬 키프레임을 캡셔닝한 것으로 영어일 수 있다.
- `화면 텍스트:`는 키프레임 OCR 결과로 오인식이 있을 수 있다.
- `챕터:`와 `내장 자막:`은 원본 컨테이너에서 추출한 구조·텍스트다.
- 발화 줄의 (SPEAKER_NN)은 화자 분리 라벨이다. 라벨이 없으면 화자 미상이다.
- 발화 텍스트는 음성 인식 결과로 일부 오인식이 있을 수 있다."""


def _fmt_ts(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    return f"{m:02d}:{s:02d}"


def _card_lines(card: dict) -> list[str]:
    lines = [
        f"### 씬 {card['scene_id']:02d} "
        f"[{_fmt_ts(card['start_sec'])} ~ {_fmt_ts(card['end_sec'])}]"
    ]
    chapter = card.get("chapter")
    if isinstance(chapter, dict) and chapter.get("title"):
        lines.append(f"챕터: {chapter['title']}")
    if card.get("subtitle_text"):
        lines.append(f"내장 자막: {card['subtitle_text']}")
    if card.get("caption"):
        lines.append(f"시각: {card['caption']}")
    if card.get("ocr_text"):
        lines.append(f"화면 텍스트: {card['ocr_text']}")
    if card["transcript"]:
        for transcript in card["transcript"]:
            who = (
                f" ({transcript['speaker']})"
                if transcript.get("speaker")
                else ""
            )
            lines.append(
                f"[{_fmt_ts(transcript['start_sec'])}]"
                f"{who} {transcript['text']}"
            )
    else:
        lines.append("(발화 없음)")
    return lines


def _toc_line(card: dict) -> str:
    chapter = card.get("chapter")
    chapter_title = (
        chapter.get("title") if isinstance(chapter, dict) else None
    )
    head = (
        card.get("caption")
        or card.get("ocr_text")
        or chapter_title
        or card.get("subtitle_text")
        or (
            card["transcript"][0]["text"][:40]
            if card["transcript"]
            else "(내용 없음)"
        )
    )
    return (
        f"- 씬 {card['scene_id']:02d} "
        f"[{_fmt_ts(card['start_sec'])}~{_fmt_ts(card['end_sec'])}] {head}"
    )


def _render_context(
    video_name: str,
    meta_lines: list[str],
    cards: list[dict],
    card_blocks: list[str] | None = None,
) -> str:
    blocks = card_blocks or ["\n".join(_card_lines(card)) for card in cards]
    toc_lines = [_toc_line(card) for card in cards]
    card_lines = []
    for block in blocks:
        card_lines.extend((block, ""))
    return "\n".join([
        f"# 영상 분석 컨텍스트: {video_name}",
        "",
        PREAMBLE,
        "",
        "## 영상 메타데이터",
        *meta_lines,
        "",
        "## 씬 목차",
        *toc_lines,
        "",
        "## 씬 상세 (씬 카드)",
        *card_lines,
    ]).rstrip() + "\n"


def _budget_context(
    video_name: str,
    meta_lines: list[str],
    cards: list[dict],
    *,
    counter,
    max_tokens: int,
) -> tuple[str, list[dict], dict]:
    selected = []
    blocks = []
    excluded = []
    truncated = []
    for card in cards:
        full = "\n".join(_card_lines(card))
        proposed = _render_context(
            video_name,
            meta_lines,
            selected + [card],
            blocks + [full],
        )
        if counter.count(proposed) <= max_tokens:
            selected.append(card)
            blocks.append(full)
            continue
        heading = _card_lines(card)[0]
        chapter = card.get("chapter")
        chapter_title = (
            chapter.get("title") if isinstance(chapter, dict) else None
        )
        summary = (
            card.get("caption")
            or card.get("ocr_text")
            or chapter_title
            or card.get("subtitle_text")
            or (
                card["transcript"][0]["text"]
                if card["transcript"]
                else "(내용 없음)"
            )
        )
        compact = f"{heading}\n{summary}"
        fitted = _fit_static_card(
            video_name,
            meta_lines,
            selected,
            blocks,
            card,
            compact,
            counter,
            max_tokens,
        )
        if fitted is None:
            excluded.append(card["scene_id"])
            continue
        selected.append(card)
        blocks.append(fitted)
        truncated.append(card["scene_id"])

    context = _render_context(video_name, meta_lines, selected, blocks)
    if counter.count(context) > max_tokens:
        context = counter.truncate(context, max_tokens)
    stats = {
        "tokenizer_model": counter.model_name,
        "max_tokens": max_tokens,
        "token_count": counter.count(context),
        "included_scene_ids": [card["scene_id"] for card in selected],
        "excluded_scene_ids": excluded,
        "truncated_scene_ids": truncated,
    }
    return context, selected, stats


def _fit_static_card(
    video_name,
    meta_lines,
    selected,
    blocks,
    card,
    compact,
    counter,
    max_tokens,
) -> str | None:
    base = _render_context(video_name, meta_lines, selected, blocks)
    available = max_tokens - counter.count(base) - 2
    while available > 0:
        fitted = counter.truncate(compact, available)
        if not fitted:
            return None
        proposed = _render_context(
            video_name,
            meta_lines,
            selected + [card],
            blocks + [fitted],
        )
        if counter.count(proposed) <= max_tokens:
            return fitted
        available -= 1
    return None


def run(ctx: PipelineContext) -> dict:
    log = stage_logger(NAME)
    out_dir = ctx.stage_dir(NAME)

    probe = ctx.load_json(ctx.out_root / "01_probe" / "metadata.json")["summary"]
    diarization = ctx.load_json(
        ctx.out_root / "07_diarize" / "diarization.json"
    )
    cards = ctx.load_json(
        ctx.out_root / "09_timeline" / "timeline.json"
    )["scene_cards"]

    log.info("컨텍스트 최종본 조립 시작: 씬 카드 %d개", len(cards))

    duration = probe["duration_sec"]
    speakers = diarization.get("speakers", [])
    meta_lines = [
        f"- 파일명: {ctx.video_path.name}",
        f"- 길이: {_fmt_ts(duration)} ({duration:.0f}초)",
        f"- 씬 수: {len(cards)}",
    ]
    if speakers:
        meta_lines.append(f"- 감지된 화자: {len(speakers)}명 ({', '.join(speakers)})")
    else:
        meta_lines.append("- 감지된 화자: 정보 없음 (화자 분리 미수행)")

    selected_cards = cards
    budget_stats = None
    if ctx.max_context_tokens is None:
        context_md = _render_context(ctx.video_path.name, meta_lines, cards)
    else:
        if ctx.context_token_counter is None:
            raise RuntimeError("context token counter is not configured")
        context_md, selected_cards, budget_stats = _budget_context(
            ctx.video_path.name,
            meta_lines,
            cards,
            counter=ctx.context_token_counter,
            max_tokens=ctx.max_context_tokens,
        )

    md_path = out_dir / "context.md"
    md_path.write_text(context_md, encoding="utf-8")

    chars = len(context_md)
    est_tokens = int(chars / 2.5)  # 한국어 혼합 텍스트 대략치
    if budget_stats is None:
        log.info(
            "context.md 저장: %d자 (추정 %d토큰, 원본 영상 %.1fMB 대비)",
            chars,
            est_tokens,
            probe["size_bytes"] / 1e6,
        )
    else:
        log.info(
            "context.md 저장: %d/%d 실제 토큰, scene %d/%d 포함",
            budget_stats["token_count"],
            budget_stats["max_tokens"],
            len(selected_cards),
            len(cards),
        )

    ctx.save_json(out_dir / "context.json", {
        "video": ctx.video_path.name,
        "duration_sec": duration,
        "speakers": speakers,
        "preamble": PREAMBLE,
        "metadata": meta_lines,
        "toc": [_toc_line(card) for card in selected_cards],
        "scene_cards": selected_cards,
        "stats": {
            "chars": chars,
            "est_tokens": est_tokens,
            "token_budget": budget_stats,
        },
    })
    result = {"chars": chars, "est_tokens": est_tokens}
    if budget_stats is not None:
        result.update(
            {
                "token_count": budget_stats["token_count"],
                "max_context_tokens": budget_stats["max_tokens"],
                "included_scene_count": len(selected_cards),
                "excluded_scene_count": len(cards) - len(selected_cards),
            }
        )
    return result
