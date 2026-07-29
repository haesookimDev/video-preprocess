"""5단계: Silero VAD로 음성 구간을 검출한다 (faster-whisper 내장 ONNX 모델 사용).

입력: 04_audio/audio_16k.wav
출력: 05_vad/vad_segments.json
"""

from faster_whisper.audio import decode_audio
from faster_whisper.vad import VadOptions, get_speech_timestamps

from ..context import PipelineContext
from ..logging_setup import stage_logger

NAME = "05_vad"
OUTPUT = "05_vad/vad_segments.json"

SAMPLE_RATE = 16000


def run(ctx: PipelineContext) -> dict:
    log = stage_logger(NAME)
    out_dir = ctx.stage_dir(NAME)

    audio_info = ctx.load_json(ctx.out_root / "04_audio" / "audio.json")
    if not audio_info.get("has_audio"):
        log.warning("오디오 없음 — VAD 스킵")
        ctx.save_json(out_dir / "vad_segments.json",
                      {"has_audio": False, "segments": []})
        return {"segment_count": 0}

    wav_path = ctx.out_root / audio_info["path"]
    log.info("VAD 시작 (min_silence=%dms, speech_pad=%dms)",
             ctx.vad_min_silence_ms, ctx.vad_speech_pad_ms)

    audio = decode_audio(str(wav_path), sampling_rate=SAMPLE_RATE)
    total_sec = len(audio) / SAMPLE_RATE
    log.debug("오디오 로드: %d샘플 (%.1f초)", len(audio), total_sec)

    options = VadOptions(
        min_silence_duration_ms=ctx.vad_min_silence_ms,
        speech_pad_ms=ctx.vad_speech_pad_ms,
    )
    chunks = get_speech_timestamps(audio, options)

    segments = []
    for i, chunk in enumerate(chunks, start=1):
        seg = {
            "segment_id": i,
            "start_sec": round(chunk["start"] / SAMPLE_RATE, 3),
            "end_sec": round(chunk["end"] / SAMPLE_RATE, 3),
        }
        seg["duration_sec"] = round(seg["end_sec"] - seg["start_sec"], 3)
        segments.append(seg)
        log.debug("음성 구간 %02d: %7.2fs ~ %7.2fs (%.2fs)",
                  i, seg["start_sec"], seg["end_sec"], seg["duration_sec"])

    speech_sec = sum(s["duration_sec"] for s in segments)
    ratio = speech_sec / total_sec if total_sec else 0
    log.info(
        "음성 구간 %d개, 총 %.1f초 / 전체 %.1f초 (음성 비율 %.0f%%, STT 대상 %.0f%% 축소)",
        len(segments), speech_sec, total_sec, ratio * 100, (1 - ratio) * 100,
    )

    result = {
        "has_audio": True,
        "total_sec": round(total_sec, 3),
        "speech_sec": round(speech_sec, 3),
        "speech_ratio": round(ratio, 3),
        "options": {
            "min_silence_duration_ms": ctx.vad_min_silence_ms,
            "speech_pad_ms": ctx.vad_speech_pad_ms,
        },
        "segments": segments,
    }
    ctx.save_json(out_dir / "vad_segments.json", result)
    return {"segment_count": len(segments), "speech_ratio": result["speech_ratio"]}
