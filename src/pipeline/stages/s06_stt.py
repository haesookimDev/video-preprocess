"""6단계: faster-whisper로 VAD 음성 구간만 전사한다.

- VAD 세그먼트 간 간격이 짧으면 병합해 호출 횟수를 줄인다.
- 세그먼트별 오디오 슬라이스를 전사하고 타임스탬프를 원본 시간축으로 보정한다.

입력: 04_audio/audio_16k.wav, 05_vad/vad_segments.json
출력: 06_stt/transcript.json
"""

from ..context import PipelineContext
from ..logging_setup import stage_logger

NAME = "06_stt"
OUTPUT = "06_stt/transcript.json"

SAMPLE_RATE = 16000


def _merge_segments(segments: list, max_gap: float) -> list:
    """간격이 max_gap 이하인 VAD 세그먼트를 병합한다."""
    merged = []
    for seg in segments:
        if merged and seg["start_sec"] - merged[-1]["end_sec"] <= max_gap:
            merged[-1]["end_sec"] = seg["end_sec"]
            merged[-1]["source_ids"].append(seg["segment_id"])
        else:
            merged.append({
                "start_sec": seg["start_sec"],
                "end_sec": seg["end_sec"],
                "source_ids": [seg["segment_id"]],
            })
    return merged


def run(ctx: PipelineContext) -> dict:
    log = stage_logger(NAME)
    out_dir = ctx.stage_dir(NAME)

    vad = ctx.load_json(ctx.out_root / "05_vad" / "vad_segments.json")
    if not vad.get("segments"):
        log.warning("음성 구간 없음 — STT 스킵")
        ctx.save_json(out_dir / "transcript.json",
                      {"segments": [], "language": None})
        return {"transcript_count": 0}

    merged = _merge_segments(vad["segments"], ctx.stt_merge_gap_sec)
    log.info(
        "VAD 세그먼트 %d개 → 병합 후 %d개 (gap<=%.1fs)",
        len(vad["segments"]), len(merged), ctx.stt_merge_gap_sec,
    )

    audio_info = ctx.load_json(ctx.out_root / "04_audio" / "audio.json")
    if ctx.stt_service is None or ctx.artifact_registrar is None:
        raise RuntimeError("STT inference dependencies were not configured")
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
        "STT provider 호출: stt.default → %s "
        "(device=auto, compute_type=int8)",
        ctx.whisper_model,
    )
    batch = ctx.stt_service.transcribe(
        audio_ref,
        merged,
        language=ctx.language,
        beam_size=5,
        sampling_rate=SAMPLE_RATE,
        run_id=ctx.out_root.name,
        stage_run_id=NAME,
    )
    transcript = [segment.to_dict() for segment in batch.segments]
    if batch.language is not None:
        if batch.language_probability is None:
            log.info("감지 언어: %s", batch.language)
        else:
            log.info(
                "감지 언어: %s (확률 %.2f)",
                batch.language,
                batch.language_probability,
            )
    for entry in transcript:
        log.info(
            "  [%7.2fs~%7.2fs] %s",
            entry["start_sec"],
            entry["end_sec"],
            entry["text"],
        )

    elapsed = float(batch.timing.get("inference_sec", 0.0))
    total_speech = sum(
        chunk["end_sec"] - chunk["start_sec"]
        for chunk in merged
    )
    rtf = elapsed / total_speech if total_speech else 0
    log.info(
        "전사 완료: 문장 %d개, 음성 %.1f초를 %.1f초에 처리 (RTF %.2f)",
        len(transcript), total_speech, elapsed, rtf,
    )

    result = {
        "model": ctx.whisper_model,
        "provider": batch.model.provider,
        "revision": batch.model.revision,
        "runtime": batch.model.runtime,
        "language": batch.language,
        "language_probability": batch.language_probability,
        "merged_chunk_count": len(merged),
        "transcribe_elapsed_sec": round(elapsed, 2),
        "segments": transcript,
    }
    log.debug(
        "실제 STT 모델: provider=%s model=%s revision=%s runtime=%s",
        batch.model.provider,
        batch.model.name,
        batch.model.revision,
        batch.model.runtime,
    )
    ctx.save_json(out_dir / "transcript.json", result)
    return {
        "transcript_count": len(transcript),
        "language": batch.language,
    }
