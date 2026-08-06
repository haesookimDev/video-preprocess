"""Unit tests for STT segment preparation."""

from pipeline.stages.s06_stt import _merge_segments


def test_merge_segments_combines_gaps_at_threshold() -> None:
    segments = [
        {"segment_id": 1, "start_sec": 0.0, "end_sec": 1.0},
        {"segment_id": 2, "start_sec": 1.5, "end_sec": 2.0},
        {"segment_id": 3, "start_sec": 2.6, "end_sec": 3.0},
    ]

    merged = _merge_segments(segments, max_gap=0.5)

    assert merged == [
        {"start_sec": 0.0, "end_sec": 2.0, "source_ids": [1, 2]},
        {"start_sec": 2.6, "end_sec": 3.0, "source_ids": [3]},
    ]


def test_merge_segments_handles_empty_input() -> None:
    assert _merge_segments([], max_gap=0.5) == []


def test_merge_segments_does_not_mutate_source() -> None:
    segments = [
        {"segment_id": 1, "start_sec": 0.0, "end_sec": 1.0},
        {"segment_id": 2, "start_sec": 1.1, "end_sec": 2.0},
    ]
    original = [segment.copy() for segment in segments]

    _merge_segments(segments, max_gap=0.5)

    assert segments == original

