"""Compatibility checks for the current unversioned (legacy v1) JSON schema."""

import json
from pathlib import Path


FIXTURES = Path(__file__).parent / "fixtures" / "legacy_v1"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_legacy_metadata_has_fields_used_by_context_stage() -> None:
    metadata = _load("metadata.json")
    summary = metadata["summary"]

    assert summary["duration_sec"] == 30.0
    assert summary["size_bytes"] > 0
    assert summary["video"]["codec"] == "h264"


def test_legacy_timeline_has_fields_used_by_index_and_query() -> None:
    timeline = _load("timeline.json")
    card = timeline["scene_cards"][0]

    assert card["scene_id"] == 1
    assert card["start_sec"] < card["end_sec"]
    assert card["caption"]
    assert card["transcript"][0]["text"] == "음성 구간 검출을 확인합니다."

