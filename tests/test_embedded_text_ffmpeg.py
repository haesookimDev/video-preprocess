"""Native FFmpeg integration for embedded subtitle and chapter extraction."""

import shutil
import subprocess
from pathlib import Path

import pytest

from pipeline.context import PipelineContext
from pipeline.stages import s01_probe, s04_embedded_text


pytestmark = pytest.mark.integration


def test_ffmpeg_extracts_mov_text_and_ffmetadata_chapters(
    tmp_path: Path,
) -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("FFmpeg tools are not installed")

    subtitle = tmp_path / "fixture.srt"
    subtitle.write_text(
        "1\n00:00:00,500 --> 00:00:02,000\nHello embedded\n\n"
        "2\n00:00:02,000 --> 00:00:03,500\n두 번째 자막\n",
        encoding="utf-8",
    )
    chapters = tmp_path / "chapters.ffmeta"
    chapters.write_text(
        ";FFMETADATA1\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=2000\n"
        "title=Opening\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=2000\nEND=4000\n"
        "title=Closing\n",
        encoding="utf-8",
    )
    video = tmp_path / "fixture.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x240:r=25:d=4",
            "-i",
            str(subtitle),
            "-f",
            "ffmetadata",
            "-i",
            str(chapters),
            "-map",
            "0:v",
            "-map",
            "1:s",
            "-map_chapters",
            "2",
            "-c:v",
            "mpeg4",
            "-c:s",
            "mov_text",
            "-t",
            "4",
            "-y",
            str(video),
        ],
        capture_output=True,
        check=True,
    )
    context = PipelineContext(
        video_path=video,
        out_root=tmp_path / "output" / "fixture",
    )

    s01_probe.run(context)
    metrics = s04_embedded_text.run(context)
    payload = context.load_json(
        context.out_root / "04_embedded_text" / "embedded_text.json"
    )

    assert metrics["subtitle_cue_count"] == 2
    assert metrics["chapter_count"] == 2
    assert [cue["text"] for cue in payload["subtitles"]] == [
        "Hello embedded",
        "두 번째 자막",
    ]
    assert [cue["start_sec"] for cue in payload["subtitles"]] == [0.5, 2.0]
    assert [chapter["title"] for chapter in payload["chapters"]] == [
        "Opening",
        "Closing",
    ]
