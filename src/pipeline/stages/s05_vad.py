"""5단계: Provider를 통해 Silero VAD 음성 구간을 검출한다.

입력: 04_audio/audio_16k.wav
출력: 05_vad/vad_segments.json
"""

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
        ctx.save_json(
            out_dir / "vad_segments.json",
            {"has_audio": False, "segments": []},
        )
        return {"segment_count": 0}

    if ctx.vad_service is None or ctx.artifact_registrar is None:
        raise RuntimeError("VAD inference dependencies were not configured")
    audio_ref = ctx.artifact_registrar.register_file(
        audio_info["path"],
        artifact_id="audio_16k",
        kind="audio",
        media_type="audio/wav",
        metadata={
            "stage": "04_audio",
            "sample_rate": audio_info["sample_rate"],
            "channels": audio_info["channels"],
            "duration_sec": audio_info["duration_sec"],
        },
    )

    log.info(
        "VAD provider 호출: vad.default → silero-vad-v6 "
        "(min_silence=%dms, speech_pad=%dms)",
        ctx.vad_min_silence_ms,
        ctx.vad_speech_pad_ms,
    )
    batch = ctx.vad_service.detect(
        audio_ref,
        min_silence_duration_ms=ctx.vad_min_silence_ms,
        speech_pad_ms=ctx.vad_speech_pad_ms,
        sampling_rate=SAMPLE_RATE,
        run_id=ctx.out_root.name,
        stage_run_id=NAME,
    )
    segments = [segment.to_dict() for segment in batch.segments]
    log.debug(
        "오디오 로드: %d샘플 (%.1f초)",
        int(batch.usage.get("sample_count", 0)),
        batch.total_sec,
    )
    for segment in segments:
        log.debug(
            "음성 구간 %02d: %7.2fs ~ %7.2fs (%.2fs)",
            segment["segment_id"],
            segment["start_sec"],
            segment["end_sec"],
            segment["duration_sec"],
        )
    log.info(
        "음성 구간 %d개, 총 %.1f초 / 전체 %.1f초 "
        "(음성 비율 %.0f%%, STT 대상 %.0f%% 축소)",
        len(segments),
        batch.speech_sec,
        batch.total_sec,
        batch.speech_ratio * 100,
        (1 - batch.speech_ratio) * 100,
    )

    result = {
        "has_audio": True,
        "model": batch.model.name,
        "provider": batch.model.provider,
        "revision": batch.model.revision,
        "runtime": batch.model.runtime,
        "total_sec": batch.total_sec,
        "speech_sec": batch.speech_sec,
        "speech_ratio": batch.speech_ratio,
        "options": {
            "min_silence_duration_ms": ctx.vad_min_silence_ms,
            "speech_pad_ms": ctx.vad_speech_pad_ms,
        },
        "segments": segments,
    }
    log.debug(
        "실제 VAD 모델: provider=%s model=%s revision=%s runtime=%s",
        batch.model.provider,
        batch.model.name,
        batch.model.revision,
        batch.model.runtime,
    )
    ctx.save_json(out_dir / "vad_segments.json", result)
    return {
        "segment_count": len(segments),
        "speech_ratio": batch.speech_ratio,
    }
