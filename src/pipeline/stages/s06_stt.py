"""6단계: faster-whisper로 VAD 음성 구간만 전사한다.

- VAD 세그먼트 간 간격이 짧으면 병합해 호출 횟수를 줄인다.
- 세그먼트별 오디오 슬라이스를 전사하고 타임스탬프를 원본 시간축으로 보정한다.

입력: 04_audio/audio_16k.wav, 05_vad/vad_segments.json
출력: 06_stt/transcript.json
"""

import time

from faster_whisper import WhisperModel
from faster_whisper.audio import decode_audio

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

    log.info("Whisper 모델 로드: %s (device=auto, compute_type=int8)",
             ctx.whisper_model)
    t0 = time.monotonic()
    model = WhisperModel(ctx.whisper_model, device="auto", compute_type="int8")
    log.debug("모델 로드 완료 (%.1fs)", time.monotonic() - t0)

    audio_info = ctx.load_json(ctx.out_root / "04_audio" / "audio.json")
    audio = decode_audio(str(ctx.out_root / audio_info["path"]),
                         sampling_rate=SAMPLE_RATE)

    transcript = []
    detected_lang = None
    total_speech = 0.0
    t_start = time.monotonic()

    for i, chunk in enumerate(merged, start=1):
        s = int(chunk["start_sec"] * SAMPLE_RATE)
        e = int(chunk["end_sec"] * SAMPLE_RATE)
        chunk_dur = chunk["end_sec"] - chunk["start_sec"]
        total_speech += chunk_dur
        log.debug("청크 %02d/%d 전사 시작: %.2fs ~ %.2fs (%.1fs)",
                  i, len(merged), chunk["start_sec"], chunk["end_sec"], chunk_dur)

        t_chunk = time.monotonic()
        segments, info = model.transcribe(
            audio[s:e], language=ctx.language, beam_size=5,
        )
        if detected_lang is None:
            detected_lang = info.language
            log.info("감지 언어: %s (확률 %.2f)",
                     info.language, info.language_probability)

        for seg in segments:
            entry = {
                "start_sec": round(chunk["start_sec"] + seg.start, 3),
                "end_sec": round(chunk["start_sec"] + seg.end, 3),
                "text": seg.text.strip(),
                "avg_logprob": round(seg.avg_logprob, 4),
                "no_speech_prob": round(seg.no_speech_prob, 4),
                "vad_source_ids": chunk["source_ids"],
            }
            transcript.append(entry)
            log.info("  [%7.2fs~%7.2fs] %s",
                     entry["start_sec"], entry["end_sec"], entry["text"])
        log.debug("청크 %02d 전사 완료 (%.1fs 소요)",
                  i, time.monotonic() - t_chunk)

    elapsed = time.monotonic() - t_start
    rtf = elapsed / total_speech if total_speech else 0
    log.info(
        "전사 완료: 문장 %d개, 음성 %.1f초를 %.1f초에 처리 (RTF %.2f)",
        len(transcript), total_speech, elapsed, rtf,
    )

    result = {
        "model": ctx.whisper_model,
        "language": detected_lang,
        "merged_chunk_count": len(merged),
        "transcribe_elapsed_sec": round(elapsed, 2),
        "segments": transcript,
    }
    ctx.save_json(out_dir / "transcript.json", result)
    return {"transcript_count": len(transcript), "language": detected_lang}
