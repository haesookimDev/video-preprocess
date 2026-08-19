"""Unit tests for timeline alignment helpers."""

import json
from pathlib import Path

from pipeline.context import PipelineContext
from pipeline.stages import s09_timeline
from pipeline.stages.s09_timeline import _assign_transcript, _match_speaker


FIXTURE = Path(__file__).parent / "fixtures" / "timeline_boundaries.json"
VISUAL_FIXTURE = Path(__file__).parent / "fixtures" / "adaptive_visuals.json"


def test_match_speaker_selects_largest_overlap() -> None:
    segment = {"start_sec": 4.0, "end_sec": 8.0}
    turns = [
        {"start_sec": 3.0, "end_sec": 5.0, "speaker": "SPEAKER_00"},
        {"start_sec": 5.0, "end_sec": 8.0, "speaker": "SPEAKER_01"},
    ]

    assert _match_speaker(segment, turns) == "SPEAKER_01"


def test_match_speaker_returns_none_without_overlap() -> None:
    segment = {"start_sec": 4.0, "end_sec": 8.0}
    turns = [
        {"start_sec": 0.0, "end_sec": 3.0, "speaker": "SPEAKER_00"},
        {"start_sec": 9.0, "end_sec": 10.0, "speaker": "SPEAKER_01"},
    ]

    assert _match_speaker(segment, turns) is None


def test_match_speaker_uses_half_open_midpoint_when_overlap_is_tied() -> None:
    segment = {"start_sec": 4.0, "end_sec": 8.0}
    turns = [
        {"start_sec": 4.0, "end_sec": 6.0, "speaker": "SPEAKER_00"},
        {"start_sec": 6.0, "end_sec": 8.0, "speaker": "SPEAKER_01"},
    ]

    assert _match_speaker(segment, turns) == "SPEAKER_01"


def test_transcripts_are_assigned_once_at_half_open_scene_boundary() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    assigned, unassigned = _assign_transcript(
        fixture["scenes"],
        fixture["transcript"],
        fixture["speaker_turns"],
    )

    assert unassigned == []
    assert [line["source_segment_id"] for line in assigned[1]] == ["left"]
    assert [line["source_segment_id"] for line in assigned[2]] == [
        "boundary-tie",
        "right",
    ]
    all_source_ids = [
        line["source_segment_id"]
        for lines in assigned.values()
        for line in lines
    ]
    assert sorted(all_source_ids) == ["boundary-tie", "left", "right"]
    boundary = assigned[2][0]
    assert boundary["speaker"] == "SPEAKER_01"
    assert boundary["avg_logprob"] == -0.2
    assert boundary["no_speech_prob"] == 0.02
    assert boundary["vad_source_ids"] == [2, 3]


def test_transcript_without_positive_scene_overlap_is_reported() -> None:
    scenes = [{"scene_id": 1, "start_sec": 0.0, "end_sec": 5.0}]
    transcript = [
        {"start_sec": 5.0, "end_sec": 6.0, "text": "범위 밖"},
        {"start_sec": 2.0, "end_sec": 2.0, "text": "길이 없음"},
    ]

    assigned, unassigned = _assign_transcript(scenes, transcript, [])

    assert assigned == {1: []}
    assert unassigned == [1, 2]


def test_timeline_preserves_multi_visuals_and_legacy_scene_summary(
    tmp_path: Path,
) -> None:
    fixture = json.loads(VISUAL_FIXTURE.read_text(encoding="utf-8"))
    context = PipelineContext(
        video_path=tmp_path / "sample.mp4",
        out_root=tmp_path / "output" / "sample",
    )
    context.save_json(
        context.stage_dir("02_scenes") / "scenes.json",
        {"scenes": fixture["scenes"]},
    )
    context.save_json(
        context.stage_dir("03_keyframes") / "keyframes.json",
        {"keyframes": fixture["keyframes"]},
    )
    context.save_json(
        context.stage_dir("06_stt") / "transcript.json",
        {"segments": []},
    )
    context.save_json(
        context.stage_dir("07_diarize") / "diarization.json",
        {"turns": []},
    )
    context.save_json(
        context.stage_dir("08_captions") / "captions.json",
        {"captions": fixture["captions"]},
    )
    context.save_json(
        context.stage_dir("08_ocr") / "ocr.json",
        {
            "results": [
                {
                    "scene_id": 1,
                    "keyframe_index": 1,
                    "keyframe_count": 2,
                    "timestamp_sec": fixture["keyframes"][0]["timestamp_sec"],
                    "keyframe": fixture["keyframes"][0]["path"],
                    "text": "OPENAI",
                    "image_width": 640,
                    "image_height": 360,
                    "regions": [
                        {
                            "region_id": 1,
                            "text": "OPENAI",
                            "confidence": 0.98,
                            "bbox": {
                                "x": 10,
                                "y": 20,
                                "width": 100,
                                "height": 30,
                            },
                        }
                    ],
                    "trigger_hint": "title",
                }
            ]
        },
    )

    metrics = s09_timeline.run(context)

    timeline = context.load_json(
        context.out_root / "09_timeline" / "timeline.json"
    )
    first, second = timeline["scene_cards"]
    assert metrics["scene_card_count"] == 2
    assert timeline["visual_summary_policy"] == (
        "ordered_unique_caption_join"
    )
    assert first["keyframe"] == fixture["keyframes"][0]["path"]
    assert first["caption"] == "a title card | a presenter"
    assert first["keyframes"] == [
        fixture["keyframes"][0]["path"],
        fixture["keyframes"][1]["path"],
    ]
    assert first["visual_captions"] == [
        {
            key: caption[key]
            for key in (
                "keyframe_index",
                "keyframe_count",
                "timestamp_sec",
                "keyframe",
                "caption",
            )
        }
        for caption in fixture["captions"][:2]
    ]
    assert second["keyframe"] == fixture["keyframes"][2]["path"]
    assert second["caption"] == "a closing frame"
    assert first["ocr_text"] == "OPENAI"
    assert first["visual_ocr"][0]["regions"][0]["confidence"] == 0.98
    assert first["visual_ocr"][0]["trigger_hint"] == "title"
    assert second["ocr_text"] is None
    assert metrics["scenes_with_ocr"] == 1
    markdown = (
        context.out_root / "09_timeline" / "timeline.md"
    ).read_text(encoding="utf-8")
    assert "시각 1/2 [00:03]: a title card" in markdown
    assert "시각 2/2 [00:06]: a presenter" in markdown
    assert "- 화면 텍스트: OPENAI" in markdown
    assert "- 시각: a closing frame" in markdown
