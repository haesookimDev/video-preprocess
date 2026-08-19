"""1단계: ffprobe로 컨테이너 메타데이터를 추출한다.

출력: 01_probe/metadata.json (raw ffprobe 결과 + 요약)
"""

import json
import subprocess

from ..context import PipelineContext
from ..logging_setup import stage_logger

NAME = "01_probe"
OUTPUT = "01_probe/metadata.json"


def run(ctx: PipelineContext) -> dict:
    log = stage_logger(NAME)
    out_dir = ctx.stage_dir(NAME)

    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", "-show_chapters",
        str(ctx.video_path),
    ]
    log.debug("실행 명령: %s", " ".join(cmd))
    raw = json.loads(subprocess.run(cmd, capture_output=True, check=True).stdout)

    fmt = raw.get("format", {})
    duration = float(fmt.get("duration", 0))
    video_streams = [s for s in raw["streams"] if s["codec_type"] == "video"]
    audio_streams = [s for s in raw["streams"] if s["codec_type"] == "audio"]
    subtitle_streams = [s for s in raw["streams"] if s["codec_type"] == "subtitle"]

    summary = {
        "path": str(ctx.video_path),
        "duration_sec": duration,
        "size_bytes": int(fmt.get("size", 0)),
        "container": fmt.get("format_name"),
        "chapters": len(raw.get("chapters", [])),
        "video": None,
        "audio": None,
        "subtitle_tracks": len(subtitle_streams),
    }
    if video_streams:
        v = video_streams[0]
        num, den = v.get("avg_frame_rate", "0/1").split("/")
        fps = float(num) / float(den) if float(den) else 0.0
        summary["video"] = {
            "codec": v.get("codec_name"),
            "width": v.get("width"),
            "height": v.get("height"),
            "fps": round(fps, 3),
        }
        log.info(
            "비디오: %s %sx%s @ %.2ffps",
            v.get("codec_name"), v.get("width"), v.get("height"), fps,
        )
    else:
        log.warning("비디오 스트림이 없습니다")

    if audio_streams:
        a = audio_streams[0]
        summary["audio"] = {
            "codec": a.get("codec_name"),
            "sample_rate": int(a.get("sample_rate", 0)),
            "channels": a.get("channels"),
        }
        log.info(
            "오디오: %s %sHz %sch",
            a.get("codec_name"), a.get("sample_rate"), a.get("channels"),
        )
    else:
        log.warning("오디오 스트림이 없습니다 — VAD/STT 단계가 스킵됩니다")

    log.info(
        "길이 %.1f초, 챕터 %d개, 내장 자막 %d개",
        duration, summary["chapters"], summary["subtitle_tracks"],
    )
    if summary["subtitle_tracks"]:
        log.info("내장 자막 발견 — 04_embedded_text 정규화 대상")

    ctx.save_json(out_dir / "metadata.json", {"summary": summary, "raw": raw})
    return summary
