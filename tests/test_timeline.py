"""Unit tests for timeline alignment helpers."""

from pipeline.stages.s09_timeline import _match_speaker


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


def test_match_speaker_keeps_first_turn_when_overlap_is_tied() -> None:
    segment = {"start_sec": 4.0, "end_sec": 8.0}
    turns = [
        {"start_sec": 4.0, "end_sec": 6.0, "speaker": "SPEAKER_00"},
        {"start_sec": 6.0, "end_sec": 8.0, "speaker": "SPEAKER_01"},
    ]

    assert _match_speaker(segment, turns) == "SPEAKER_00"

