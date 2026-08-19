"""9단계: 시각·OCR·전사·화자를 공통 시간축으로 병합한다.

- 모든 시간 구간은 반개구간 ``[start_sec, end_sec)``로 해석한다.
- 전사 세그먼트는 가장 많이 겹치는 씬 하나에만 귀속한다.
- 겹침이 같으면 세그먼트 중점을 포함하는 구간, 그 다음 입력 순서를 사용한다.
- 화자 라벨도 같은 최대 겹침·중점 규칙으로 정렬한다.

입력: 02_scenes, 03_keyframes, 06_stt, 07_diarize, 08_captions, 08_ocr 산출물
출력:
- 09_timeline/timeline.json : 씬 카드 목록 (LLM 입력 조립의 기본 단위)
- 09_timeline/timeline.md   : 사람이 읽는 확인용 뷰
"""

import math

from ..context import PipelineContext
from ..logging_setup import stage_logger

NAME = "09_timeline"
OUTPUT = "09_timeline/timeline.json"
INTERVAL_CONVENTION = "[start_sec,end_sec)"
ASSIGNMENT_POLICY = "maximum_overlap_single_midpoint_tiebreak"
VISUAL_SUMMARY_POLICY = "ordered_unique_caption_join"
OCR_SUMMARY_POLICY = "ordered_unique_ocr_text_join"


def _overlap_duration(left: dict, right: dict) -> float:
    """Return the positive overlap of two half-open time intervals."""

    return max(
        0.0,
        min(float(left["end_sec"]), float(right["end_sec"]))
        - max(float(left["start_sec"]), float(right["start_sec"])),
    )


def _contains(interval: dict, timestamp: float) -> bool:
    """Return whether a timestamp belongs to a half-open interval."""

    return (
        float(interval["start_sec"])
        <= timestamp
        < float(interval["end_sec"])
    )


def _best_overlap_index(segment: dict, intervals: list[dict]) -> int | None:
    """Select exactly one interval using overlap, midpoint and stable order."""

    overlaps = [
        (index, _overlap_duration(segment, interval))
        for index, interval in enumerate(intervals)
    ]
    positive = [(index, overlap) for index, overlap in overlaps if overlap > 0]
    if not positive:
        return None
    maximum = max(overlap for _, overlap in positive)
    tied = [
        index
        for index, overlap in positive
        if math.isclose(overlap, maximum, rel_tol=1e-9, abs_tol=1e-9)
    ]
    if len(tied) == 1:
        return tied[0]

    midpoint = (
        float(segment["start_sec"]) + float(segment["end_sec"])
    ) / 2.0
    for index in tied:
        if _contains(intervals[index], midpoint):
            return index
    return tied[0]


def _match_speaker(seg: dict, turns: list) -> str | None:
    """전사 세그먼트와 가장 잘 정렬되는 화자 턴의 화자를 반환한다."""

    index = _best_overlap_index(seg, turns)
    return None if index is None else turns[index]["speaker"]


def _transcript_line(seg: dict, source_segment_id: object, turns: list) -> dict:
    """Preserve source identity and confidence in a timeline transcript line."""

    line = {
        "start_sec": seg["start_sec"],
        "end_sec": seg["end_sec"],
        "speaker": _match_speaker(seg, turns),
        "text": seg["text"],
        "source_segment_id": source_segment_id,
    }
    for field_name in ("vad_source_ids", "avg_logprob", "no_speech_prob"):
        if field_name in seg:
            line[field_name] = seg[field_name]
    return line


def _assign_transcript(
    scenes: list[dict],
    transcript: list[dict],
    speaker_turns: list[dict],
) -> tuple[dict[object, list[dict]], list[object]]:
    """Assign each positive-duration transcript to at most one scene."""

    assigned = {scene["scene_id"]: [] for scene in scenes}
    unassigned = []
    for index, seg in enumerate(transcript, start=1):
        source_segment_id = seg.get("segment_id", index)
        scene_index = _best_overlap_index(seg, scenes)
        if scene_index is None:
            unassigned.append(source_segment_id)
            continue
        scene_id = scenes[scene_index]["scene_id"]
        assigned[scene_id].append(
            _transcript_line(seg, source_segment_id, speaker_turns)
        )
    return assigned, unassigned


def _fmt_ts(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    return f"{m:02d}:{s:02d}"


def _group_by_scene(entries: list[dict]) -> dict[object, list[dict]]:
    """Group entries without losing their deterministic input order."""

    grouped: dict[object, list[dict]] = {}
    for entry in entries:
        grouped.setdefault(entry["scene_id"], []).append(entry)
    return grouped


def _visual_scene_fields(
    scene: dict,
    keyframes: list[dict],
    captions: list[dict],
    ocr_results: list[dict],
) -> dict:
    """Build additive multi-visual fields and compatible scalar summaries."""

    midpoint = (
        float(scene["start_sec"]) + float(scene["end_sec"])
    ) / 2.0
    representative = min(
        keyframes,
        key=lambda entry: abs(float(entry["timestamp_sec"]) - midpoint),
        default=None,
    )
    visual_captions = []
    for position, caption in enumerate(captions, start=1):
        visual_captions.append({
            "keyframe_index": caption.get("keyframe_index", position),
            "keyframe_count": caption.get("keyframe_count", len(captions)),
            "timestamp_sec": caption["timestamp_sec"],
            "keyframe": caption["keyframe"],
            "caption": caption["caption"],
        })
    unique_captions = []
    for visual in visual_captions:
        caption = visual["caption"].strip()
        if caption not in unique_captions:
            unique_captions.append(caption)
    visual_ocr = []
    unique_ocr_texts = []
    for position, result in enumerate(ocr_results, start=1):
        entry = {
            "keyframe_index": result.get("keyframe_index", position),
            "keyframe_count": result.get(
                "keyframe_count",
                len(ocr_results),
            ),
            "timestamp_sec": result["timestamp_sec"],
            "keyframe": result["keyframe"],
            "text": result["text"],
            "image_width": result["image_width"],
            "image_height": result["image_height"],
            "regions": result["regions"],
        }
        if "trigger_hint" in result:
            entry["trigger_hint"] = result["trigger_hint"]
        visual_ocr.append(entry)
        text = result["text"].strip()
        if text and text not in unique_ocr_texts:
            unique_ocr_texts.append(text)
    return {
        "keyframe": None if representative is None else representative["path"],
        "caption": " | ".join(unique_captions) or None,
        "keyframes": [entry["path"] for entry in keyframes],
        "visual_captions": visual_captions,
        "ocr_text": " | ".join(unique_ocr_texts) or None,
        "visual_ocr": visual_ocr,
    }


def run(ctx: PipelineContext) -> dict:
    log = stage_logger(NAME)
    out_dir = ctx.stage_dir(NAME)

    scenes = ctx.load_json(ctx.out_root / "02_scenes" / "scenes.json")["scenes"]
    keyframes = _group_by_scene(ctx.load_json(
        ctx.out_root / "03_keyframes" / "keyframes.json"
    )["keyframes"])
    caption_entries = ctx.load_json(
        ctx.out_root / "08_captions" / "captions.json"
    )["captions"]
    captions = _group_by_scene(caption_entries)
    ocr_payload = ctx.load_json(ctx.out_root / "08_ocr" / "ocr.json")
    ocr_entries = ocr_payload.get("results", [])
    if not isinstance(ocr_entries, list):
        raise ValueError("08_ocr results must be an array")
    ocr_results = _group_by_scene(ocr_entries)
    transcript = ctx.load_json(
        ctx.out_root / "06_stt" / "transcript.json"
    )["segments"]
    diarization = ctx.load_json(
        ctx.out_root / "07_diarize" / "diarization.json"
    )
    speaker_turns = diarization.get("turns", [])

    log.info(
        "타임라인 병합 시작: 씬 %d개, 캡션 %d개, OCR %d개, "
        "전사 세그먼트 %d개, 화자 턴 %d개",
        len(scenes),
        len(caption_entries),
        len(ocr_entries),
        len(transcript),
        len(speaker_turns),
    )

    assigned, unassigned = _assign_transcript(
        scenes,
        transcript,
        speaker_turns,
    )
    cards = []
    for scene in scenes:
        lines = assigned[scene["scene_id"]]
        visual_fields = _visual_scene_fields(
            scene,
            keyframes.get(scene["scene_id"], []),
            captions.get(scene["scene_id"], []),
            ocr_results.get(scene["scene_id"], []),
        )

        card = {
            "scene_id": scene["scene_id"],
            "start_sec": scene["start_sec"],
            "end_sec": scene["end_sec"],
            "duration_sec": scene["duration_sec"],
            **visual_fields,
            "transcript": lines,
        }
        cards.append(card)
        log.debug("씬 %02d 카드: 캡션 %s, 발화 %d줄",
                  scene["scene_id"],
                  "있음" if card["caption"] else "없음", len(lines))

    if unassigned:
        log.warning(
            "씬과 겹치지 않은 전사 세그먼트 %d개: %s",
            len(unassigned),
            unassigned,
        )

    with_speech = sum(1 for c in cards if c["transcript"])
    log.info("씬 카드 %d개 생성 (발화 포함 씬 %d개)", len(cards), with_speech)

    ctx.save_json(
        out_dir / "timeline.json",
        {
            "interval_convention": INTERVAL_CONVENTION,
            "transcript_assignment": ASSIGNMENT_POLICY,
            "visual_summary_policy": VISUAL_SUMMARY_POLICY,
            "ocr_summary_policy": OCR_SUMMARY_POLICY,
            "source_transcript_count": len(transcript),
            "assigned_transcript_count": len(transcript) - len(unassigned),
            "unassigned_source_segment_ids": unassigned,
            "scene_cards": cards,
        },
    )

    md_lines = [f"# 타임라인: {ctx.video_path.name}", ""]
    for card in cards:
        md_lines.append(
            f"## 씬 {card['scene_id']:02d} "
            f"[{_fmt_ts(card['start_sec'])} ~ {_fmt_ts(card['end_sec'])}]"
        )
        visuals = card["visual_captions"]
        if len(visuals) == 1:
            md_lines.append(f"- 시각: {visuals[0]['caption']}")
        elif visuals:
            for visual in visuals:
                md_lines.append(
                    f"- 시각 {visual['keyframe_index']}/"
                    f"{visual['keyframe_count']} "
                    f"[{_fmt_ts(visual['timestamp_sec'])}]: "
                    f"{visual['caption']}"
                )
        if len(card["keyframes"]) == 1:
            md_lines.append(f"- 키프레임: `{card['keyframes'][0]}`")
        elif card["keyframes"]:
            for index, keyframe in enumerate(card["keyframes"], start=1):
                md_lines.append(
                    f"- 키프레임 {index}/{len(card['keyframes'])}: "
                    f"`{keyframe}`"
                )
        ocr_visuals = [
            visual for visual in card["visual_ocr"] if visual["text"]
        ]
        if len(ocr_visuals) == 1:
            md_lines.append(
                "- 화면 텍스트: "
                + ocr_visuals[0]["text"].replace("\n", " / ")
            )
        elif ocr_visuals:
            for visual in ocr_visuals:
                md_lines.append(
                    f"- 화면 텍스트 {visual['keyframe_index']}/"
                    f"{visual['keyframe_count']}: "
                    + visual["text"].replace("\n", " / ")
                )
        if card["transcript"]:
            for line in card["transcript"]:
                who = f" ({line['speaker']})" if line.get("speaker") else ""
                md_lines.append(
                    f"- [{_fmt_ts(line['start_sec'])}]{who} {line['text']}"
                )
        else:
            md_lines.append("- (발화 없음)")
        md_lines.append("")
    (out_dir / "timeline.md").write_text("\n".join(md_lines), encoding="utf-8")
    log.debug("확인용 마크다운 저장: %s", out_dir / "timeline.md")

    with_ocr = sum(1 for card in cards if card["ocr_text"])
    return {
        "scene_card_count": len(cards),
        "scenes_with_speech": with_speech,
        "assigned_transcript_count": len(transcript) - len(unassigned),
        "unassigned_transcript_count": len(unassigned),
        "scenes_with_ocr": with_ocr,
    }
