"""4단계 보조 분기: 내장 텍스트 자막과 챕터를 정규화한다.

텍스트 기반 자막은 FFmpeg로 WebVTT에 변환한 뒤 cue로 파싱한다. 비트맵 자막은
OCR과 책임이 다르므로 이 단계에서 명시적으로 건너뛴다.

출력: 04_embedded_text/embedded_text.json
"""

from __future__ import annotations

import html
import math
import re
import subprocess
from collections.abc import Mapping

from ..context import PipelineContext
from ..logging_setup import stage_logger

NAME = "04_embedded_text"
OUTPUT = "04_embedded_text/embedded_text.json"
SCHEMA_VERSION = "1"
INTERVAL_CONVENTION = "[start_sec,end_sec)"
EXTRACTION_POLICY = "ffmpeg-webvtt-text-subtitles-v1"

SUPPORTED_SUBTITLE_CODECS = frozenset(
    {
        "ass",
        "mov_text",
        "ssa",
        "srt",
        "subrip",
        "text",
        "ttml",
        "webvtt",
    }
)

_TIMING_PATTERN = re.compile(
    r"^(?P<start>\S+)\s+-->\s+(?P<end>\S+)(?:\s+.*)?$"
)
_VTT_TAG_PATTERN = re.compile(r"<[^>]*>")
_ASS_OVERRIDE_PATTERN = re.compile(r"\{\\[^}]*\}")


def _parse_timestamp(value: str) -> float:
    """Parse a WebVTT ``HH:MM:SS.mmm`` or ``MM:SS.mmm`` timestamp."""

    parts = value.split(":")
    if len(parts) not in {2, 3}:
        raise ValueError("invalid WebVTT timestamp")
    try:
        seconds = float(parts[-1])
        minutes = int(parts[-2])
        hours = int(parts[-3]) if len(parts) == 3 else 0
    except ValueError as exc:
        raise ValueError("invalid WebVTT timestamp") from exc
    result = hours * 3600 + minutes * 60 + seconds
    if hours < 0 or not 0 <= minutes < 60 or not 0 <= seconds < 60:
        raise ValueError("invalid WebVTT timestamp")
    if not math.isfinite(result):
        raise ValueError("invalid WebVTT timestamp")
    return round(result, 3)


def _plain_text(lines: list[str]) -> str:
    """Remove WebVTT/ASS presentation markup while preserving line order."""

    normalized = []
    for line in lines:
        text = _ASS_OVERRIDE_PATTERN.sub("", line)
        text = _VTT_TAG_PATTERN.sub("", text)
        text = html.unescape(text)
        text = " ".join(text.split())
        if text:
            normalized.append(text)
    return "\n".join(normalized)


def parse_webvtt(payload: str) -> list[dict]:
    """Return normalized cue intervals from FFmpeg-generated WebVTT."""

    if not isinstance(payload, str):
        raise TypeError("WebVTT payload must be text")
    lines = payload.removeprefix("\ufeff").replace("\r\n", "\n").split("\n")
    first_content = next((line.strip() for line in lines if line.strip()), "")
    if not first_content.startswith("WEBVTT"):
        raise ValueError("WebVTT payload is missing its header")

    blocks = []
    current = []
    for line in lines:
        if line.strip():
            current.append(line)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)

    cues = []
    for block in blocks[1:]:
        first = block[0].strip()
        if first.startswith(("NOTE", "STYLE", "REGION")):
            continue
        timing_index = next(
            (index for index, line in enumerate(block[:2]) if "-->" in line),
            None,
        )
        if timing_index is None:
            continue
        match = _TIMING_PATTERN.match(block[timing_index].strip())
        if match is None:
            raise ValueError("invalid WebVTT cue timing")
        start_sec = _parse_timestamp(match.group("start"))
        end_sec = _parse_timestamp(match.group("end"))
        if end_sec <= start_sec:
            raise ValueError("WebVTT cue must have positive duration")
        text = _plain_text(block[timing_index + 1 :])
        if not text:
            continue
        cues.append(
            {
                "start_sec": start_sec,
                "end_sec": end_sec,
                "text": text,
            }
        )
    return cues


def _optional_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _stream_descriptor(stream: Mapping[str, object], position: int) -> dict:
    stream_index = stream.get("index")
    if isinstance(stream_index, bool) or not isinstance(stream_index, int):
        raise ValueError("subtitle stream index must be an integer")
    codec_name = _optional_text(stream.get("codec_name")) or "unknown"
    tags = stream.get("tags", {})
    if not isinstance(tags, Mapping):
        tags = {}
    disposition = stream.get("disposition", {})
    if not isinstance(disposition, Mapping):
        disposition = {}
    return {
        "source_id": f"subtitle:stream:{stream_index}",
        "stream_index": stream_index,
        "subtitle_index": position,
        "codec_name": codec_name,
        "language": _optional_text(tags.get("language")),
        "title": _optional_text(tags.get("title")),
        "disposition": {
            name: bool(disposition.get(name, 0))
            for name in ("default", "forced", "hearing_impaired")
        },
    }


def _chapter_interval(chapter: Mapping[str, object]) -> tuple[float, float]:
    try:
        start_sec = float(chapter["start_time"])
        end_sec = float(chapter["end_time"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("chapter must contain numeric start/end times") from exc
    if (
        not math.isfinite(start_sec)
        or not math.isfinite(end_sec)
        or end_sec <= start_sec
    ):
        raise ValueError("chapter must have a finite positive duration")
    return round(start_sec, 3), round(end_sec, 3)


def _normalize_chapters(raw_chapters: object) -> list[dict]:
    if not isinstance(raw_chapters, list):
        raise ValueError("ffprobe chapters must be an array")
    chapters = []
    for chapter_index, raw in enumerate(raw_chapters):
        if not isinstance(raw, Mapping):
            raise ValueError("ffprobe chapter must be an object")
        start_sec, end_sec = _chapter_interval(raw)
        tags = raw.get("tags", {})
        if not isinstance(tags, Mapping):
            tags = {}
        source_chapter_id = raw.get("id", chapter_index)
        if not isinstance(source_chapter_id, (str, int)) or isinstance(
            source_chapter_id,
            bool,
        ):
            source_chapter_id = chapter_index
        chapters.append(
            {
                "source_id": f"chapter:{chapter_index}",
                "chapter_index": chapter_index,
                "source_chapter_id": source_chapter_id,
                "start_sec": start_sec,
                "end_sec": end_sec,
                "title": (
                    _optional_text(tags.get("title"))
                    or f"Chapter {chapter_index + 1}"
                ),
                "language": _optional_text(tags.get("language")),
            }
        )
    return chapters


def _extract_webvtt(video_path, stream_index: int) -> str:
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-nostdin",
        "-copyts",
        "-start_at_zero",
        "-i",
        str(video_path),
        "-map",
        f"0:{stream_index}",
        "-c:s",
        "webvtt",
        "-f",
        "webvtt",
        "pipe:1",
    ]
    try:
        completed = subprocess.run(command, capture_output=True, check=True)
        return completed.stdout.decode("utf-8")
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"subtitle stream {stream_index} could not be converted to WebVTT"
        ) from exc
    except UnicodeDecodeError as exc:
        raise RuntimeError(
            f"subtitle stream {stream_index} produced invalid UTF-8"
        ) from exc


def run(ctx: PipelineContext) -> dict:
    log = stage_logger(NAME)
    out_dir = ctx.stage_dir(NAME)
    metadata = ctx.load_json(ctx.out_root / "01_probe" / "metadata.json")
    raw = metadata.get("raw", {})
    if not isinstance(raw, Mapping):
        raise ValueError("01_probe raw metadata must be an object")
    raw_streams = raw.get("streams", [])
    if not isinstance(raw_streams, list):
        raise ValueError("ffprobe streams must be an array")

    raw_subtitles = [
        stream
        for stream in raw_streams
        if isinstance(stream, Mapping) and stream.get("codec_type") == "subtitle"
    ]
    chapters = _normalize_chapters(raw.get("chapters", []))
    streams = []
    subtitles = []
    extracted_stream_count = 0
    for subtitle_index, raw_stream in enumerate(raw_subtitles):
        descriptor = _stream_descriptor(raw_stream, subtitle_index)
        codec_name = descriptor["codec_name"]
        if codec_name not in SUPPORTED_SUBTITLE_CODECS:
            descriptor.update(
                {
                    "status": "skipped",
                    "reason_code": "UNSUPPORTED_SUBTITLE_CODEC",
                    "cue_count": 0,
                }
            )
            streams.append(descriptor)
            log.warning(
                "자막 stream %d의 codec %s는 텍스트 추출 범위 밖입니다",
                descriptor["stream_index"],
                codec_name,
            )
            continue

        cue_payloads = parse_webvtt(
            _extract_webvtt(ctx.video_path, descriptor["stream_index"])
        )
        extracted_stream_count += 1
        descriptor.update(
            {
                "status": "extracted",
                "reason_code": None,
                "cue_count": len(cue_payloads),
            }
        )
        streams.append(descriptor)
        for cue_index, cue in enumerate(cue_payloads, start=1):
            subtitles.append(
                {
                    "source_id": (
                        f"{descriptor['source_id']}:cue:{cue_index}"
                    ),
                    "source_stream_id": descriptor["source_id"],
                    "cue_index": cue_index,
                    "stream_index": descriptor["stream_index"],
                    "language": descriptor["language"],
                    **cue,
                }
            )

    subtitles.sort(
        key=lambda cue: (
            cue["start_sec"],
            cue["end_sec"],
            cue["stream_index"],
            cue["cue_index"],
        )
    )
    executed = bool(extracted_stream_count or chapters)
    reason_code = None
    if not raw_subtitles and not chapters:
        reason_code = "NO_EMBEDDED_TEXT"
    elif not executed:
        reason_code = "NO_EXTRACTABLE_EMBEDDED_TEXT"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "interval_convention": INTERVAL_CONVENTION,
        "extraction_policy": EXTRACTION_POLICY,
        "executed": executed,
        "available": bool(subtitles or chapters),
        "reason_code": reason_code,
        "subtitle_streams": streams,
        "subtitles": subtitles,
        "chapters": chapters,
        "stats": {
            "subtitle_stream_count": len(streams),
            "extracted_stream_count": extracted_stream_count,
            "unsupported_stream_count": len(streams) - extracted_stream_count,
            "subtitle_cue_count": len(subtitles),
            "chapter_count": len(chapters),
        },
    }
    ctx.save_json(out_dir / "embedded_text.json", payload)
    log.info(
        "내장 텍스트 정규화: 자막 stream %d개(cue %d개), 챕터 %d개",
        len(streams),
        len(subtitles),
        len(chapters),
    )
    return dict(payload["stats"])
