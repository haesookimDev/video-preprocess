"""파이프라인 러너: 단계를 순차 실행하고 시간·결과를 기록한다.

- 각 단계의 대표 출력 파일이 이미 있으면 스킵 (--force로 강제 재실행)
- 실행 요약을 output/<video>/run_summary.json 에 저장
"""

import time
import traceback
from datetime import datetime

from .context import PipelineContext
from .logging_setup import setup_logging
from .stages import s01_probe, s02_scenes, s03_keyframes, s04_audio, s05_vad, \
    s06_stt, s07_timeline

STAGES = [s01_probe, s02_scenes, s03_keyframes, s04_audio, s05_vad, s06_stt,
          s07_timeline]


def run_pipeline(ctx: PipelineContext) -> dict:
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = ctx.log_dir / f"run_{run_id}.log"
    log = setup_logging(log_file)

    log.info("=" * 60)
    log.info("파이프라인 시작: %s", ctx.video_path.name)
    log.info("출력 디렉토리: %s", ctx.out_root)
    log.info("설정: %s", ctx)
    log.info("=" * 60)

    summary = {
        "run_id": run_id,
        "video": str(ctx.video_path),
        "log_file": str(log_file),
        "stages": [],
    }
    pipeline_start = time.monotonic()

    for stage in STAGES:
        marker = ctx.out_root / stage.OUTPUT
        record = {"name": stage.NAME, "output": stage.OUTPUT}

        if marker.exists() and not ctx.force:
            log.info("[%s] 기존 출력 존재 → 스킵 (%s). 재실행: --force",
                     stage.NAME, stage.OUTPUT)
            record["status"] = "skipped"
            summary["stages"].append(record)
            continue

        log.info("--- [%s] 시작 ---", stage.NAME)
        t0 = time.monotonic()
        try:
            result = stage.run(ctx)
        except Exception:
            elapsed = time.monotonic() - t0
            log.error("[%s] 실패 (%.1fs)\n%s",
                      stage.NAME, elapsed, traceback.format_exc())
            record.update({"status": "failed",
                           "elapsed_sec": round(elapsed, 2)})
            summary["stages"].append(record)
            summary["status"] = "failed"
            break
        elapsed = time.monotonic() - t0
        log.info("--- [%s] 완료 (%.1fs) → %s ---",
                 stage.NAME, elapsed, result)
        record.update({"status": "ok", "elapsed_sec": round(elapsed, 2),
                       "result": result})
        summary["stages"].append(record)
    else:
        summary["status"] = "ok"

    total = time.monotonic() - pipeline_start
    summary["total_elapsed_sec"] = round(total, 2)
    ctx.save_json(ctx.out_root / "run_summary.json", summary)

    log.info("=" * 60)
    log.info("파이프라인 종료: %s (총 %.1fs)", summary["status"], total)
    for r in summary["stages"]:
        log.info("  %-13s %-8s %6ss", r["name"], r["status"],
                 r.get("elapsed_sec", "-"))
    log.info("실행 요약: %s", ctx.out_root / "run_summary.json")
    log.info("상세 로그: %s", log_file)
    return summary
