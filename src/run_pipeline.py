"""영상 전처리 최소 파이프라인 CLI.

사용법:
    python src/run_pipeline.py <video> [--out output] [--force]
        [--whisper-model base] [--language ko] [--scene-threshold 27]
"""

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sys
from dataclasses import replace
from pathlib import Path

from pipeline.preflight import format_report, run_preflight


def main() -> int:
    parser = argparse.ArgumentParser(description="영상 전처리 최소 파이프라인")
    parser.add_argument("video", nargs="?", type=Path, help="입력 영상 경로")
    parser.add_argument("--out", type=Path, default=Path("output"),
                        help="출력 루트 디렉토리 (기본: output)")
    parser.add_argument("--force", action="store_true",
                        help="기존 단계 출력이 있어도 전부 재실행")
    parser.add_argument("--whisper-model", default="base",
                        help="faster-whisper 모델 크기 (기본: base)")
    parser.add_argument("--language", default=None,
                        help="전사 언어 코드 (기본: 자동 감지)")
    parser.add_argument("--scene-threshold", type=float, default=27.0,
                        help="씬 검출 임계값 (기본: 27.0)")
    parser.add_argument(
        "--keyframes-per-scene",
        type=int,
        default=1,
        help="씬 길이별 adaptive 키프레임 최대 수, 1~3 (기본: 1)",
    )
    parser.add_argument(
        "--max-context-tokens",
        type=int,
        default=None,
        help="11_context 실제 token 상한 (기본: 제한 없음)",
    )
    parser.add_argument(
        "--context-tokenizer-model",
        default=None,
        help="context token 계산용 Hugging Face tokenizer model",
    )
    parser.add_argument("--run-id", default=None,
                        help="재개할 run ID (기본: 출력 경로 기반 local ID)")
    parser.add_argument("--stage", default=None,
                        help="정확히 한 단계만 실행")
    parser.add_argument("--from-stage", default=None,
                        help="지정 단계와 그 하위 단계를 실행")
    parser.add_argument("--to-stage", default=None,
                        help="지정 단계까지 필요한 상위 단계를 실행")
    parser.add_argument("--force-stage", action="append", default=[],
                        help="캐시를 무시할 단계 (여러 번 지정 가능)")
    parser.add_argument("--dry-run", action="store_true",
                        help="실행 없이 단계 plan과 boundary input 출력")
    parser.add_argument("--stage-timeout-sec", type=float, default=None,
                        help="Stage별 timeout 초 (기본: 제한 없음)")
    parser.add_argument("--max-stage-attempts", type=int, default=1,
                        help="일시적 실패의 Stage 최대 시도 수 (기본: 1)")
    parser.add_argument("--retry-backoff-sec", type=float, default=0.0,
                        help="첫 재시도 전 대기 초 (기본: 0)")
    parser.add_argument(
        "--executor-max-concurrency",
        type=int,
        default=1,
        help="동시에 실행할 local Stage 수 (기본: 1)",
    )
    parser.add_argument(
        "--caption-device",
        default="auto",
        help="local caption device: auto, cpu, cuda, mps 등 (기본: auto)",
    )
    parser.add_argument(
        "--caption-batch-size",
        type=int,
        default=4,
        help="local caption ordered chunk 크기 (기본: 4)",
    )
    parser.add_argument(
        "--ocr-mode",
        choices=("disabled", "all", "caption-hints"),
        default="disabled",
        help="OCR trigger: disabled, all, caption-hints (기본: disabled)",
    )
    parser.add_argument(
        "--ocr-model",
        default="tesseract",
        help="OCR provider model 이름 (기본: tesseract)",
    )
    parser.add_argument(
        "--ocr-language",
        action="append",
        default=[],
        help="Tesseract language ID. 여러 번 지정 가능 (기본: eng)",
    )
    parser.add_argument(
        "--ocr-min-confidence",
        type=float,
        default=0.5,
        help="OCR word confidence 하한 0~1 (기본: 0.5)",
    )
    parser.add_argument(
        "--ocr-detect-orientation",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="OCR orientation detection 사용 (기본: true)",
    )
    parser.add_argument(
        "--ocr-command",
        default="tesseract",
        help="local OCR command (기본: tesseract)",
    )
    parser.add_argument(
        "--ocr-batch-size",
        type=int,
        default=4,
        help="local OCR ordered chunk 크기 (기본: 4)",
    )
    parser.add_argument("--ocr-endpoint", default=None,
                        help="ocr.default HTTP Inference v1 endpoint")
    parser.add_argument("--ocr-token-env", default=None,
                        help="OCR HTTP bearer token 환경변수 이름")
    parser.add_argument(
        "--ocr-artifact-namespace",
        action="append",
        default=[],
        help="원격 OCR이 접근할 Artifact Store namespace",
    )
    parser.add_argument("--embedding-endpoint", default=None,
                        help="embedding.default HTTP Inference v1 endpoint")
    parser.add_argument("--embedding-token-env", default=None,
                        help="HTTP bearer token을 읽을 환경변수 이름")
    parser.add_argument(
        "--embedding-artifact-namespace",
        action="append",
        default=[],
        help="원격 embedding이 접근할 Artifact Store namespace",
    )
    parser.add_argument("--preflight-only", action="store_true",
                        help="실행 환경만 검사하고 종료")
    args = parser.parse_args()

    if args.video is None and not args.preflight_only:
        parser.error("video 인수가 필요합니다")

    project_root = Path(__file__).resolve().parents[1]
    report = run_preflight(project_root)
    if args.preflight_only:
        print(format_report(report))
        return 0 if report.ok else 1

    if report.warnings or report.errors:
        print(format_report(report, include_ok=False), file=sys.stderr)
    if not report.ok:
        return 1

    if (
        args.ocr_mode != "disabled"
        and args.ocr_endpoint is None
        and shutil.which(args.ocr_command) is None
    ):
        print(
            f"오류: OCR command를 찾을 수 없습니다: {args.ocr_command}",
            file=sys.stderr,
        )
        return 1

    if not args.video.exists():
        print(f"오류: 영상 파일이 없습니다: {args.video}", file=sys.stderr)
        return 1

    # 무거운 단계 모듈은 runtime factory가 실제 실행 시점에만 로드한다.
    from video_preprocess.engine import DAGPlanner, create_default_registry
    from pipeline.deployment import inference_deployments_from_environment
    from video_preprocess.services import (
        LocalPipelineRuntimeFactory,
        PipelineApplicationService,
        PipelineRunRequest,
        PipelineSettings,
    )

    output_root = (args.out / args.video.stem).resolve()
    run_id = args.run_id or _local_run_id(output_root)
    try:
        deployments = inference_deployments_from_environment(
            endpoints={
                "embedding.default": args.embedding_endpoint,
                "ocr.default": args.ocr_endpoint,
            },
            token_envs={
                "embedding.default": args.embedding_token_env,
                "ocr.default": args.ocr_token_env,
            },
            artifact_namespaces={
                "embedding.default": args.embedding_artifact_namespace,
                "ocr.default": args.ocr_artifact_namespace,
            },
            environ=os.environ,
        )
        request = PipelineRunRequest(
            video_path=args.video.resolve(),
            output_root=output_root,
            run_id=run_id,
            stage=args.stage,
            from_stage=args.from_stage,
            to_stage=args.to_stage,
            force_stages=tuple(args.force_stage),
            stage_timeout_sec=args.stage_timeout_sec,
            max_stage_attempts=args.max_stage_attempts,
            retry_backoff_sec=args.retry_backoff_sec,
            deployments=deployments,
            settings=PipelineSettings(
                whisper_model=args.whisper_model,
                language=args.language,
                scene_threshold=args.scene_threshold,
                keyframes_per_scene=args.keyframes_per_scene,
                ocr_mode=args.ocr_mode,
                ocr_model=args.ocr_model,
                ocr_languages=tuple(args.ocr_language or ("eng",)),
                ocr_detect_orientation=args.ocr_detect_orientation,
                ocr_min_confidence=args.ocr_min_confidence,
                max_context_tokens=args.max_context_tokens,
                context_tokenizer_model=args.context_tokenizer_model,
            ),
        )
    except (TypeError, ValueError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    try:
        service = PipelineApplicationService(
            DAGPlanner(create_default_registry()),
            LocalPipelineRuntimeFactory(
                project_root=project_root,
                executor_max_concurrency=args.executor_max_concurrency,
                caption_device=args.caption_device,
                caption_batch_size=args.caption_batch_size,
                ocr_command=args.ocr_command,
                ocr_batch_size=args.ocr_batch_size,
            ),
        )
        plan = service.plan(request)
        if args.force:
            forced = tuple(sorted(set(request.force_stages) | set(plan.stage_names)))
            request = replace(request, force_stages=forced)
        if args.dry_run:
            preview = asyncio.run(service.preview(request))
            print(json.dumps(
                {
                    "run_id": run_id,
                    "stages": list(plan.stage_names),
                    "boundary_inputs": list(plan.boundary_inputs),
                    "force_stages": list(request.force_stages),
                    "execution_policy": {
                        "stage_timeout_sec": request.stage_timeout_sec,
                        "max_stage_attempts": request.max_stage_attempts,
                        "retry_backoff_sec": request.retry_backoff_sec,
                        "executor_max_concurrency": (
                            args.executor_max_concurrency
                        ),
                    },
                    "inference_deployments": (
                        request.deployments.public_dict()
                    ),
                    "local_inference": {
                        "caption_device": args.caption_device,
                        "caption_batch_size": args.caption_batch_size,
                        "ocr_command": args.ocr_command,
                        "ocr_batch_size": args.ocr_batch_size,
                    },
                    "cache_decisions": [
                        _preview_stage_payload(record)
                        for record in preview.stages
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ))
            return 0
        result = asyncio.run(service.run(request))
    except (TypeError, ValueError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2

    summary = _write_compatibility_summary(
        result,
        video_path=args.video.resolve(),
        output_root=output_root,
    )
    print(
        f"파이프라인 종료: {summary['status']} "
        f"(run_id={result.run_id}, stages={len(result.stages)})"
    )
    return 0 if summary["status"] == "ok" else 1


def _preview_stage_payload(record) -> dict[str, object]:
    reasons = []
    if record.cache_decision is not None:
        reasons.extend(
            {
                "code": miss.reason.value,
                "subject": miss.subject,
                "detail": miss.detail,
            }
            for miss in record.cache_decision.misses
        )
    reasons.extend(
        {
            "code": "REQUIRED_INPUT_UNAVAILABLE",
            "subject": input_name,
            "detail": None,
        }
        for input_name in record.blocked_inputs
    )
    return {
        "stage": record.stage,
        "status": record.status.value,
        "will_execute": record.will_execute,
        "reasons": reasons,
    }


def _local_run_id(output_root: Path) -> str:
    digest = hashlib.sha256(
        str(output_root.resolve()).encode("utf-8")
    ).hexdigest()[:20]
    return f"local_{digest}"


def _write_compatibility_summary(
    result,
    *,
    video_path: Path,
    output_root: Path,
) -> dict[str, object]:
    stages = []
    for record in result.stages:
        status = record.result.status.value
        if record.from_cache:
            status = "cached"
        elif status == "succeeded":
            status = "ok"
        stages.append(
            {
                "name": record.stage,
                "attempt": record.task.attempt,
                "status": status,
                "result": dict(record.result.metrics),
                "outputs": {
                    name: ref.uri
                    for name, ref in record.result.outputs.items()
                },
                "cache": (
                    None
                    if record.cache_decision is None
                    else record.cache_decision.status.value
                ),
            }
        )
    summary = {
        "run_id": result.run_id,
        "video": str(video_path),
        "log_file": str(output_root / "logs" / f"run_{result.run_id}.log"),
        "stages": stages,
        "status": "ok" if result.status.value == "succeeded" else result.status.value,
    }
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "run_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return summary


if __name__ == "__main__":
    sys.exit(main())
