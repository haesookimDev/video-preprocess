"""Tests for embedded subtitle and chapter normalization."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline.context import PipelineContext
from pipeline.stages import s04_embedded_text


VTT_FIXTURE = Path(__file__).parent / "fixtures" / "embedded_text.webvtt"


def _context(tmp_path: Path, raw: dict) -> PipelineContext:
    context = PipelineContext(
        video_path=tmp_path / "sample.mp4",
        out_root=tmp_path / "output" / "sample",
    )
    context.video_path.write_bytes(b"video")
    context.save_json(
        context.stage_dir("01_probe") / "metadata.json",
        {"summary": {}, "raw": raw},
    )
    return context


def test_webvtt_parser_preserves_intervals_and_removes_markup() -> None:
    cues = s04_embedded_text.parse_webvtt(
        VTT_FIXTURE.read_text(encoding="utf-8")
    )

    assert cues == [
        {
            "start_sec": 0.5,
            "end_sec": 4.0,
            "text": "Hello & welcome",
        },
        {
            "start_sec": 4.0,
            "end_sec": 7.5,
            "text": "두 번째 자막\nsecond line",
        },
    ]


@pytest.mark.parametrize(
    "payload",
    [
        "not-vtt",
        "WEBVTT\n\n00:01.000 --> invalid\ntext\n",
        "WEBVTT\n\n00:02.000 --> 00:02.000\ntext\n",
    ],
)
def test_webvtt_parser_rejects_invalid_contract(payload: str) -> None:
    with pytest.raises(ValueError):
        s04_embedded_text.parse_webvtt(payload)


def test_stage_extracts_supported_stream_and_normalizes_chapters(
    tmp_path: Path,
    monkeypatch,
) -> None:
    context = _context(
        tmp_path,
        {
            "streams": [
                {"index": 0, "codec_type": "video", "codec_name": "h264"},
                {
                    "index": 2,
                    "codec_type": "subtitle",
                    "codec_name": "mov_text",
                    "tags": {"language": "eng", "title": "English"},
                    "disposition": {"default": 1, "forced": 0},
                },
                {
                    "index": 3,
                    "codec_type": "subtitle",
                    "codec_name": "hdmv_pgs_subtitle",
                    "tags": {"language": "kor"},
                },
            ],
            "chapters": [
                {
                    "id": 10,
                    "start_time": "0.000000",
                    "end_time": "5.000000",
                    "tags": {"title": "Opening", "language": "eng"},
                },
                {
                    "id": 20,
                    "start_time": "5.000000",
                    "end_time": "10.000000",
                    "tags": {},
                },
            ],
        },
    )
    commands = []

    def fake_run(command, *, capture_output, check):
        commands.append(command)
        assert capture_output is True
        assert check is True
        return SimpleNamespace(stdout=VTT_FIXTURE.read_bytes())

    monkeypatch.setattr(s04_embedded_text.subprocess, "run", fake_run)

    metrics = s04_embedded_text.run(context)

    payload = json.loads(
        (
            context.out_root
            / "04_embedded_text"
            / "embedded_text.json"
        ).read_text(encoding="utf-8")
    )
    assert metrics == {
        "subtitle_stream_count": 2,
        "extracted_stream_count": 1,
        "unsupported_stream_count": 1,
        "subtitle_cue_count": 2,
        "chapter_count": 2,
    }
    assert payload["schema_version"] == "1"
    assert payload["interval_convention"] == "[start_sec,end_sec)"
    assert payload["extraction_policy"] == (
        "ffmpeg-webvtt-text-subtitles-v1"
    )
    assert payload["executed"] is True
    assert payload["available"] is True
    assert payload["reason_code"] is None
    assert payload["subtitle_streams"] == [
        {
            "source_id": "subtitle:stream:2",
            "stream_index": 2,
            "subtitle_index": 0,
            "codec_name": "mov_text",
            "language": "eng",
            "title": "English",
            "disposition": {
                "default": True,
                "forced": False,
                "hearing_impaired": False,
            },
            "status": "extracted",
            "reason_code": None,
            "cue_count": 2,
        },
        {
            "source_id": "subtitle:stream:3",
            "stream_index": 3,
            "subtitle_index": 1,
            "codec_name": "hdmv_pgs_subtitle",
            "language": "kor",
            "title": None,
            "disposition": {
                "default": False,
                "forced": False,
                "hearing_impaired": False,
            },
            "status": "skipped",
            "reason_code": "UNSUPPORTED_SUBTITLE_CODEC",
            "cue_count": 0,
        },
    ]
    assert [cue["source_id"] for cue in payload["subtitles"]] == [
        "subtitle:stream:2:cue:1",
        "subtitle:stream:2:cue:2",
    ]
    assert payload["subtitles"][0]["language"] == "eng"
    assert payload["chapters"] == [
        {
            "source_id": "chapter:0",
            "chapter_index": 0,
            "source_chapter_id": 10,
            "start_sec": 0.0,
            "end_sec": 5.0,
            "title": "Opening",
            "language": "eng",
        },
        {
            "source_id": "chapter:1",
            "chapter_index": 1,
            "source_chapter_id": 20,
            "start_sec": 5.0,
            "end_sec": 10.0,
            "title": "Chapter 2",
            "language": None,
        },
    ]
    assert len(commands) == 1
    assert commands[0][commands[0].index("-map") + 1] == "0:2"
    assert commands[0][-3:] == ["-f", "webvtt", "pipe:1"]


def test_stage_writes_skip_sentinel_without_embedded_text(
    tmp_path: Path,
    monkeypatch,
) -> None:
    context = _context(tmp_path, {"streams": [], "chapters": []})
    monkeypatch.setattr(
        s04_embedded_text.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("FFmpeg must not be called"),
    )

    metrics = s04_embedded_text.run(context)
    payload = context.load_json(
        context.out_root / "04_embedded_text" / "embedded_text.json"
    )

    assert metrics["subtitle_cue_count"] == 0
    assert payload["executed"] is False
    assert payload["available"] is False
    assert payload["reason_code"] == "NO_EMBEDDED_TEXT"
    assert payload["subtitle_streams"] == []
    assert payload["subtitles"] == []
    assert payload["chapters"] == []


def test_bitmap_only_stream_uses_stable_skip_reason(
    tmp_path: Path,
    monkeypatch,
) -> None:
    context = _context(
        tmp_path,
        {
            "streams": [
                {
                    "index": 4,
                    "codec_type": "subtitle",
                    "codec_name": "dvd_subtitle",
                }
            ],
            "chapters": [],
        },
    )
    monkeypatch.setattr(
        s04_embedded_text.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("FFmpeg must not be called"),
    )

    s04_embedded_text.run(context)
    payload = context.load_json(
        context.out_root / "04_embedded_text" / "embedded_text.json"
    )

    assert payload["executed"] is False
    assert payload["reason_code"] == "NO_EXTRACTABLE_EMBEDDED_TEXT"
    assert payload["subtitle_streams"][0]["reason_code"] == (
        "UNSUPPORTED_SUBTITLE_CODEC"
    )
