"""7단계: Provider를 통해 화자 분리(diarization)를 수행한다.

- 오디오가 없거나 credential/게이트 접근이 없으면 빈 결과를 저장하고 스킵한다.
- Stage는 배포 위치나 pyannote 모델 생명주기를 알지 않는다.

입력: 04_audio/audio_16k.wav
출력: 07_diarize/diarization.json
"""

from pathlib import Path

from video_preprocess.domain import InferenceErrorCode, InferenceFailure
from video_preprocess.inference import InferenceCallError

from ..context import PipelineContext
from ..logging_setup import stage_logger

NAME = "07_diarize"
OUTPUT = "07_diarize/diarization.json"


def _skip(ctx: PipelineContext, out_dir: Path, reason: str, log) -> dict:
    log.warning("%s — 화자 분리 스킵", reason)
    ctx.save_json(
        out_dir / "diarization.json",
        {"available": False, "reason": reason, "turns": []},
    )
    return {"speaker_count": 0, "skipped": reason}


def _skip_reason(
    failure: InferenceFailure,
    model_name: str,
) -> str | None:
    code = failure.code
    if code is InferenceErrorCode.AUTHENTICATION_FAILED:
        if failure.details.get("reason") == "CREDENTIAL_MISSING":
            return "환경변수/.env에 HF_TOKEN 없음"
        return "HF_TOKEN 인증 실패"
    if code is InferenceErrorCode.MODEL_ACCESS_DENIED:
        return (
            f"게이트 모델 접근 거부(403): {model_name} — "
            "HF 토큰에 게이트 리포 읽기 권한이 있는지, 모델 약관에 "
            "동의했는지 확인"
        )
    if code is InferenceErrorCode.MODEL_UNAVAILABLE:
        return f"모델 로드 실패: {model_name}"
    return None


def run(ctx: PipelineContext) -> dict:
    log = stage_logger(NAME)
    out_dir = ctx.stage_dir(NAME)

    audio_info = ctx.load_json(ctx.out_root / "04_audio" / "audio.json")
    if not audio_info.get("has_audio"):
        return _skip(ctx, out_dir, "오디오 없음", log)

    if ctx.diarization_service is None or ctx.artifact_registrar is None:
        raise RuntimeError(
            "diarization inference dependencies were not configured"
        )
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
        "화자 분리 provider 호출: diarization.default → %s",
        ctx.diarize_model,
    )
    try:
        batch = ctx.diarization_service.diarize(
            audio_ref,
            run_id=ctx.out_root.name,
            stage_run_id=NAME,
        )
    except InferenceCallError as exc:
        reason = _skip_reason(exc.failure, ctx.diarize_model)
        if reason is None:
            raise
        return _skip(ctx, out_dir, reason, log)

    turns = [turn.to_dict() for turn in batch.turns]
    for turn in turns:
        log.debug(
            "턴 %02d: %7.2fs ~ %7.2fs %s",
            turn["turn_id"],
            turn["start_sec"],
            turn["end_sec"],
            turn["speaker"],
        )
    elapsed = float(batch.timing.get("inference_sec", 0.0))
    log.info(
        "화자 분리 완료 (%.1fs): 화자 %d명, 발화 턴 %d개 %s",
        elapsed,
        len(batch.speakers),
        len(turns),
        list(batch.speakers),
    )
    ctx.save_json(
        out_dir / "diarization.json",
        {
            "available": True,
            "model": ctx.diarize_model,
            "provider": batch.model.provider,
            "revision": batch.model.revision,
            "runtime": batch.model.runtime,
            "speakers": list(batch.speakers),
            "turns": turns,
        },
    )
    log.debug(
        "실제 diarization 모델: provider=%s model=%s revision=%s runtime=%s",
        batch.model.provider,
        batch.model.name,
        batch.model.revision,
        batch.model.runtime,
    )
    return {
        "speaker_count": len(batch.speakers),
        "turn_count": len(turns),
    }
