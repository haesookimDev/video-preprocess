"""4단계: 오디오 트랙을 디먹싱해 16kHz mono WAV로 정규화한다.

출력:
- 04_audio/audio_16k.wav
- 04_audio/audio.json
"""

import subprocess

from ..context import PipelineContext
from ..logging_setup import stage_logger

NAME = "04_audio"
OUTPUT = "04_audio/audio.json"


def run(ctx: PipelineContext) -> dict:
    log = stage_logger(NAME)
    out_dir = ctx.stage_dir(NAME)

    probe = ctx.load_json(ctx.out_root / "01_probe" / "metadata.json")["summary"]
    if probe["audio"] is None:
        log.warning("오디오 스트림 없음 — 빈 결과 저장 후 스킵")
        ctx.save_json(out_dir / "audio.json", {"has_audio": False})
        return {"has_audio": False}

    wav_path = out_dir / "audio_16k.wav"
    cmd = [
        "ffmpeg", "-v", "error", "-i", str(ctx.video_path),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        "-y", str(wav_path),
    ]
    log.debug("실행 명령: %s", " ".join(cmd))
    log.info(
        "오디오 정규화: %s %dHz %dch → pcm_s16le 16000Hz 1ch",
        probe["audio"]["codec"], probe["audio"]["sample_rate"],
        probe["audio"]["channels"],
    )
    subprocess.run(cmd, capture_output=True, check=True)

    size = wav_path.stat().st_size
    duration = (size - 44) / (16000 * 2)  # WAV 헤더 제외, 16bit mono
    log.info("추출 완료: %s (%.1fMB, %.1f초)", wav_path.name, size / 1e6, duration)

    result = {
        "has_audio": True,
        "path": str(wav_path.relative_to(ctx.out_root)),
        "sample_rate": 16000,
        "channels": 1,
        "duration_sec": round(duration, 3),
        "size_bytes": size,
    }
    ctx.save_json(out_dir / "audio.json", result)
    return {"has_audio": True, "duration_sec": result["duration_sec"]}
