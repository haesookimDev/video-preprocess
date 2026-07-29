"""8단계: 씬·키프레임·캡션·전사문을 공통 시간축으로 병합해 씬 카드를 생성한다.

- 전사 세그먼트는 씬과의 겹침(overlap) 시간 기준으로 귀속한다.

입력: 02_scenes, 03_keyframes, 06_stt, 07_captions 산출물
출력:
- 08_timeline/timeline.json : 씬 카드 목록 (LLM 입력 조립의 기본 단위)
- 08_timeline/timeline.md   : 사람이 읽는 확인용 뷰
"""

from ..context import PipelineContext
from ..logging_setup import stage_logger

NAME = "08_timeline"
OUTPUT = "08_timeline/timeline.json"


def _fmt_ts(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    return f"{m:02d}:{s:02d}"


def run(ctx: PipelineContext) -> dict:
    log = stage_logger(NAME)
    out_dir = ctx.stage_dir(NAME)

    scenes = ctx.load_json(ctx.out_root / "02_scenes" / "scenes.json")["scenes"]
    keyframes = {
        k["scene_id"]: k
        for k in ctx.load_json(
            ctx.out_root / "03_keyframes" / "keyframes.json"
        )["keyframes"]
    }
    captions = {
        c["scene_id"]: c["caption"]
        for c in ctx.load_json(
            ctx.out_root / "07_captions" / "captions.json"
        )["captions"]
    }
    transcript = ctx.load_json(
        ctx.out_root / "06_stt" / "transcript.json"
    )["segments"]

    log.info("타임라인 병합 시작: 씬 %d개, 캡션 %d개, 전사 세그먼트 %d개",
             len(scenes), len(captions), len(transcript))

    cards = []
    assigned = set()
    for scene in scenes:
        lines = []
        for idx, seg in enumerate(transcript):
            overlap = min(scene["end_sec"], seg["end_sec"]) - max(
                scene["start_sec"], seg["start_sec"]
            )
            seg_dur = seg["end_sec"] - seg["start_sec"]
            # 겹침이 세그먼트 절반 이상인 씬에 귀속
            if seg_dur > 0 and overlap / seg_dur >= 0.5:
                lines.append({
                    "start_sec": seg["start_sec"],
                    "end_sec": seg["end_sec"],
                    "text": seg["text"],
                })
                assigned.add(idx)

        card = {
            "scene_id": scene["scene_id"],
            "start_sec": scene["start_sec"],
            "end_sec": scene["end_sec"],
            "duration_sec": scene["duration_sec"],
            "keyframe": keyframes.get(scene["scene_id"], {}).get("path"),
            "caption": captions.get(scene["scene_id"]),
            "transcript": lines,
        }
        cards.append(card)
        log.debug("씬 %02d 카드: 캡션 %s, 발화 %d줄",
                  scene["scene_id"],
                  "있음" if card["caption"] else "없음", len(lines))

    unassigned = len(transcript) - len(assigned)
    if unassigned:
        log.warning("씬에 귀속되지 않은 전사 세그먼트 %d개 (경계 걸침)", unassigned)

    with_speech = sum(1 for c in cards if c["transcript"])
    log.info("씬 카드 %d개 생성 (발화 포함 씬 %d개)", len(cards), with_speech)

    ctx.save_json(out_dir / "timeline.json", {"scene_cards": cards})

    md_lines = [f"# 타임라인: {ctx.video_path.name}", ""]
    for card in cards:
        md_lines.append(
            f"## 씬 {card['scene_id']:02d} "
            f"[{_fmt_ts(card['start_sec'])} ~ {_fmt_ts(card['end_sec'])}]"
        )
        if card["caption"]:
            md_lines.append(f"- 시각: {card['caption']}")
        if card["keyframe"]:
            md_lines.append(f"- 키프레임: `{card['keyframe']}`")
        if card["transcript"]:
            for line in card["transcript"]:
                md_lines.append(
                    f"- [{_fmt_ts(line['start_sec'])}] {line['text']}"
                )
        else:
            md_lines.append("- (발화 없음)")
        md_lines.append("")
    (out_dir / "timeline.md").write_text("\n".join(md_lines), encoding="utf-8")
    log.debug("확인용 마크다운 저장: %s", out_dir / "timeline.md")

    return {"scene_card_count": len(cards), "scenes_with_speech": with_speech}
