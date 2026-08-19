"""Unit tests for timeline alignment helpers."""

import json
from pathlib import Path

from pipeline.context import PipelineContext
from pipeline.stages import s09_timeline
from pipeline.stages.s09_timeline import (
    _assign_subtitles,
    _assign_transcript,
    _chapter_for_scene,
    _match_speaker,
)


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


def test_embedded_text_uses_same_single_assignment_and_chapter_policy() -> None:
    scenes = [
        {"scene_id": 1, "start_sec": 0.0, "end_sec": 5.0},
        {"scene_id": 2, "start_sec": 5.0, "end_sec": 10.0},
    ]
    subtitles = [
        {
            "source_id": "subtitle:stream:2:cue:1",
            "start_sec": 4.0,
            "end_sec": 6.0,
            "text": "boundary",
        },
        {
            "source_id": "subtitle:stream:2:cue:2",
            "start_sec": 10.0,
            "end_sec": 11.0,
            "text": "outside",
        },
    ]
    chapters = [
        {
            "source_id": "chapter:0",
            "start_sec": 0.0,
            "end_sec": 5.0,
            "title": "Opening",
        },
        {
            "source_id": "chapter:1",
            "start_sec": 5.0,
            "end_sec": 10.0,
            "title": "Main",
        },
    ]

    assigned, unassigned = _assign_subtitles(scenes, subtitles)

    assert assigned[1] == []
    assert assigned[2][0]["source_id"] == "subtitle:stream:2:cue:1"
    assert unassigned == ["subtitle:stream:2:cue:2"]
    assert _chapter_for_scene(scenes[0], chapters)["source_id"] == "chapter:0"
    assert _chapter_for_scene(scenes[1], chapters)["source_id"] == "chapter:1"


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
    context.save_json(
        context.stage_dir("04_embedded_text") / "embedded_text.json",
        {
            "subtitles": [
                {
                    "source_id": "subtitle:stream:2:cue:1",
                    "source_stream_id": "subtitle:stream:2",
                    "cue_index": 1,
                    "stream_index": 2,
                    "language": "eng",
                    "start_sec": 1.0,
                    "end_sec": 4.0,
                    "text": "Welcome",
                },
                {
                    "source_id": "subtitle:stream:2:cue:2",
                    "source_stream_id": "subtitle:stream:2",
                    "cue_index": 2,
                    "stream_index": 2,
                    "language": "eng",
                    "start_sec": 9.0,
                    "end_sec": 11.0,
                    "text": "Boundary subtitle",
                },
                {
                    "source_id": "subtitle:stream:2:cue:3",
                    "source_stream_id": "subtitle:stream:2",
                    "cue_index": 3,
                    "stream_index": 2,
                    "language": "eng",
                    "start_sec": 20.0,
                    "end_sec": 22.0,
                    "text": "Outside",
                },
            ],
            "chapters": [
                {
                    "source_id": "chapter:0",
                    "chapter_index": 0,
                    "source_chapter_id": 10,
                    "start_sec": 0.0,
                    "end_sec": 8.0,
                    "title": "Opening",
                    "language": "eng",
                },
                {
                    "source_id": "chapter:1",
                    "chapter_index": 1,
                    "source_chapter_id": 20,
                    "start_sec": 8.0,
                    "end_sec": 16.0,
                    "title": "Main",
                    "language": "eng",
                },
            ],
        },
    )
    context.save_json(
        context.stage_dir("05_audio_events") / "audio_events.json",
        {
            "events": [
                {
                    "event_id": 1,
                    "label": "music",
                    "confidence": 0.92,
                    "start_sec": 1.0,
                    "end_sec": 7.0,
                    "duration_sec": 6.0,
                    "source_window_ids": [1, 2],
                },
                {
                    "event_id": 2,
                    "label": "applause",
                    "confidence": 0.88,
                    "start_sec": 20.0,
                    "end_sec": 22.0,
                    "duration_sec": 2.0,
                    "source_window_ids": [8],
                },
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
    assert timeline["subtitle_assignment"] == (
        "maximum_overlap_single_midpoint_tiebreak"
    )
    assert timeline["chapter_assignment"] == (
        "maximum_overlap_single_midpoint_tiebreak"
    )
    assert timeline["assigned_subtitle_count"] == 2
    assert timeline["assigned_audio_event_count"] == 1
    assert timeline["unassigned_audio_event_ids"] == [2]
    assert timeline["unassigned_subtitle_source_ids"] == [
        "subtitle:stream:2:cue:3"
    ]
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
    assert first["chapter"]["source_id"] == "chapter:0"
    assert second["chapter"]["source_id"] == "chapter:1"
    assert first["subtitle_text"] == "Welcome"
    assert second["subtitle_text"] == "Boundary subtitle"
    assert first["subtitles"][0]["source_stream_id"] == "subtitle:stream:2"
    assert first["audio_event_text"] == "music"
    assert first["audio_events"][0]["confidence"] == 0.92
    assert first["audio_events"][0]["source_window_ids"] == [1, 2]
    assert second["audio_event_text"] is None
    assert metrics["scenes_with_ocr"] == 1
    assert metrics["assigned_subtitle_count"] == 2
    assert metrics["unassigned_subtitle_count"] == 1
    assert metrics["scenes_with_subtitles"] == 2
    assert metrics["scenes_with_chapter"] == 2
    assert metrics["scenes_with_audio_events"] == 1
    assert metrics["unassigned_audio_event_count"] == 1
    markdown = (
        context.out_root / "09_timeline" / "timeline.md"
    ).read_text(encoding="utf-8")
    assert "시각 1/2 [00:03]: a title card" in markdown
    assert "시각 2/2 [00:06]: a presenter" in markdown
    assert "- 화면 텍스트: OPENAI" in markdown
    assert "- 챕터: Opening" in markdown
    assert "- 내장 자막 [00:01] (eng): Welcome" in markdown
    assert "- 내장 자막 [00:09] (eng): Boundary subtitle" in markdown
    assert "- 오디오 이벤트 [00:01]: music (0.92)" in markdown
    assert "- 시각: a closing frame" in markdown
