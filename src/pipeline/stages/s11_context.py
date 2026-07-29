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
- 발화 줄의 (SPEAKER_NN)은 화자 분리 라벨이다. 라벨이 없으면 화자 미상이다.
- 발화 텍스트는 음성 인식 결과로 일부 오인식이 있을 수 있다."""


def _fmt_ts(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    return f"{m:02d}:{s:02d}"


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

    toc_lines = []
    for c in cards:
        head = c["caption"] or (
            c["transcript"][0]["text"][:40] if c["transcript"] else "(내용 없음)"
        )
        toc_lines.append(
            f"- 씬 {c['scene_id']:02d} "
            f"[{_fmt_ts(c['start_sec'])}~{_fmt_ts(c['end_sec'])}] {head}"
        )

    card_lines = []
    for c in cards:
        card_lines.append(
            f"### 씬 {c['scene_id']:02d} "
            f"[{_fmt_ts(c['start_sec'])} ~ {_fmt_ts(c['end_sec'])}]"
        )
        if c["caption"]:
            card_lines.append(f"시각: {c['caption']}")
        if c["transcript"]:
            for t in c["transcript"]:
                who = f" ({t['speaker']})" if t.get("speaker") else ""
                card_lines.append(f"[{_fmt_ts(t['start_sec'])}]{who} {t['text']}")
        else:
            card_lines.append("(발화 없음)")
        card_lines.append("")

    context_md = "\n".join([
        f"# 영상 분석 컨텍스트: {ctx.video_path.name}",
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

    md_path = out_dir / "context.md"
    md_path.write_text(context_md, encoding="utf-8")

    chars = len(context_md)
    est_tokens = int(chars / 2.5)  # 한국어 혼합 텍스트 대략치
    log.info("context.md 저장: %d자 (추정 %d토큰, 원본 영상 %.1fMB 대비)",
             chars, est_tokens, probe["size_bytes"] / 1e6)

    ctx.save_json(out_dir / "context.json", {
        "video": ctx.video_path.name,
        "duration_sec": duration,
        "speakers": speakers,
        "preamble": PREAMBLE,
        "metadata": meta_lines,
        "toc": toc_lines,
        "scene_cards": cards,
        "stats": {"chars": chars, "est_tokens": est_tokens},
    })
    return {"chars": chars, "est_tokens": est_tokens}
