"""7단계: pyannote로 화자 분리(diarization)를 수행한다.

- 게이트 모델이므로 환경변수 또는 .env의 HF_TOKEN이 필요하다.
- 오디오가 없거나 토큰이 없으면 빈 결과를 저장하고 스킵한다 (후속 단계는 화자
  라벨 없이 동작).

입력: 04_audio/audio_16k.wav
출력: 07_diarize/diarization.json
"""

import time
from pathlib import Path

from ..context import PipelineContext
from ..logging_setup import stage_logger
from ..preflight import load_hf_token

NAME = "07_diarize"
OUTPUT = "07_diarize/diarization.json"


def _skip(ctx: PipelineContext, out_dir: Path, reason: str, log) -> dict:
    log.warning("%s — 화자 분리 스킵", reason)
    ctx.save_json(out_dir / "diarization.json",
                  {"available": False, "reason": reason, "turns": []})
    return {"speaker_count": 0, "skipped": reason}


def run(ctx: PipelineContext) -> dict:
    log = stage_logger(NAME)
    out_dir = ctx.stage_dir(NAME)

    audio_info = ctx.load_json(ctx.out_root / "04_audio" / "audio.json")
    if not audio_info.get("has_audio"):
        return _skip(ctx, out_dir, "오디오 없음", log)

    token = load_hf_token(Path(__file__).resolve().parents[3])
    if not token:
        return _skip(ctx, out_dir, "환경변수/.env에 HF_TOKEN 없음", log)

    log.info("화자 분리 모델 로드: %s", ctx.diarize_model)
    t0 = time.monotonic()
    from huggingface_hub.errors import GatedRepoError
    from pyannote.audio import Pipeline

    try:
        pipeline = Pipeline.from_pretrained(ctx.diarize_model, token=token)
    except GatedRepoError:
        return _skip(
            ctx, out_dir,
            f"게이트 모델 접근 거부(403): {ctx.diarize_model} — "
            "HF 토큰에 게이트 리포 읽기 권한이 있는지, 모델 약관에 동의했는지 확인",
            log,
        )
    if pipeline is None:
        return _skip(ctx, out_dir,
                     f"모델 로드 실패: {ctx.diarize_model}", log)
    log.debug("모델 로드 완료 (%.1fs)", time.monotonic() - t0)

    wav_path = ctx.out_root / audio_info["path"]
    t0 = time.monotonic()
    result = pipeline(str(wav_path))
    # pyannote.audio 4.x는 래핑된 출력, 3.x는 Annotation을 그대로 반환
    annotation = getattr(result, "speaker_diarization", result)
    elapsed = time.monotonic() - t0

    turns = []
    for i, (segment, _, speaker) in enumerate(
        annotation.itertracks(yield_label=True), start=1
    ):
        turn = {
            "turn_id": i,
            "start_sec": round(segment.start, 3),
            "end_sec": round(segment.end, 3),
            "speaker": speaker,
        }
        turns.append(turn)
        log.debug("턴 %02d: %7.2fs ~ %7.2fs %s",
                  i, turn["start_sec"], turn["end_sec"], speaker)

    speakers = sorted({t["speaker"] for t in turns})
    log.info("화자 분리 완료 (%.1fs): 화자 %d명, 발화 턴 %d개 %s",
             elapsed, len(speakers), len(turns), speakers)

    ctx.save_json(out_dir / "diarization.json", {
        "available": True,
        "model": ctx.diarize_model,
        "speakers": speakers,
        "turns": turns,
    })
    return {"speaker_count": len(speakers), "turn_count": len(turns)}
