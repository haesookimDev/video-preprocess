"""Unit tests for timeline alignment helpers."""

import json
from pathlib import Path

from pipeline.stages.s09_timeline import _assign_transcript, _match_speaker


FIXTURE = Path(__file__).parent / "fixtures" / "timeline_boundaries.json"


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
