"""선택 단계: Provider를 통해 비음성 오디오 이벤트를 검출한다.

입력: 04_audio/audio_16k.wav
출력: 05_audio_events/audio_events.json
"""

from video_preprocess.inference import (
    AUDIO_EVENT_OVERLAP_POLICY,
    AUDIO_EVENT_TAXONOMY_VERSION,
)

from ..context import PipelineContext
from ..logging_setup import stage_logger


NAME = "05_audio_events"
OUTPUT = "05_audio_events/audio_events.json"
SAMPLE_RATE = 16000


def _skip(
    ctx: PipelineContext,
    out_dir,
    *,
    reason_code: str,
    reason: str,
) -> dict[str, object]:
    ctx.save_json(out_dir / "audio_events.json", {
        "enabled": ctx.audio_event_mode != "disabled",
        "executed": False,
        "reason_code": reason_code,
        "reason": reason,
        "model": ctx.audio_event_model,
        "taxonomy_version": AUDIO_EVENT_TAXONOMY_VERSION,
        "overlap_policy": AUDIO_EVENT_OVERLAP_POLICY,
        "interval": "half-open",
        "labels": list(ctx.audio_event_labels),
        "min_confidence": ctx.audio_event_min_confidence,
        "window_sec": ctx.audio_event_window_sec,
        "hop_sec": ctx.audio_event_hop_sec,
        "events": [],
    })
    return {"audio_event_count": 0, "skipped": reason_code}


def run(ctx: PipelineContext) -> dict[str, object]:
    log = stage_logger(NAME)
    out_dir = ctx.stage_dir(NAME)
    audio_info = ctx.load_json(ctx.out_root / "04_audio" / "audio.json")

    if ctx.audio_event_mode == "disabled":
        log.info("오디오 이벤트 비활성화 — 05_audio_events 스킵")
        return _skip(
            ctx,
            out_dir,
            reason_code="AUDIO_EVENTS_DISABLED",
            reason="audio event detection is disabled by pipeline settings",
        )
    if ctx.audio_event_mode != "all":
        raise ValueError("audio_event_mode must be disabled or all")
    if not audio_info.get("has_audio"):
        log.warning("오디오 없음 — 오디오 이벤트 스킵")
        return _skip(
            ctx,
            out_dir,
            reason_code="NO_AUDIO",
            reason="audio input has no audio stream",
        )
    if ctx.audio_event_service is None or ctx.artifact_registrar is None:
        raise RuntimeError(
            "audio event inference dependencies were not configured"
        )

    audio_ref = ctx.artifact_registrar.register_file(
        audio_info["path"],
        artifact_id="audio_events_audio_16k",
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
        "오디오 이벤트 provider 호출: audio_event.default → %s",
        ctx.audio_event_model,
    )
    batch = ctx.audio_event_service.detect(
        audio_ref,
        duration_sec=audio_info["duration_sec"],
        labels=ctx.audio_event_labels,
        min_confidence=ctx.audio_event_min_confidence,
        window_sec=ctx.audio_event_window_sec,
        hop_sec=ctx.audio_event_hop_sec,
        sampling_rate=SAMPLE_RATE,
        run_id=ctx.out_root.name,
        stage_run_id=NAME,
    )
    events = [event.to_dict() for event in batch.events]
    ctx.save_json(out_dir / "audio_events.json", {
        "enabled": True,
        "executed": True,
        "model": batch.model.name,
        "provider": batch.model.provider,
        "revision": batch.model.revision,
        "runtime": batch.model.runtime,
        "taxonomy_version": AUDIO_EVENT_TAXONOMY_VERSION,
        "overlap_policy": AUDIO_EVENT_OVERLAP_POLICY,
        "interval": "half-open",
        "labels": list(ctx.audio_event_labels),
        "min_confidence": ctx.audio_event_min_confidence,
        "window_sec": ctx.audio_event_window_sec,
        "hop_sec": ctx.audio_event_hop_sec,
        "usage": batch.usage,
        "timing": batch.timing,
        "events": events,
    })
    log.info("오디오 이벤트 완료: %d개", len(events))
    return {"audio_event_count": len(events)}
