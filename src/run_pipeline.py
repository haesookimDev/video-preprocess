"""영상 전처리 최소 파이프라인 CLI.

사용법:
    python src/run_pipeline.py <video> [--out output] [--force]
        [--whisper-model base] [--language ko] [--scene-threshold 27]
"""

import argparse
import sys
from pathlib import Path

from pipeline.context import PipelineContext
from pipeline.runner import run_pipeline


def main() -> int:
    parser = argparse.ArgumentParser(description="영상 전처리 최소 파이프라인")
    parser.add_argument("video", type=Path, help="입력 영상 경로")
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
    args = parser.parse_args()

    if not args.video.exists():
        print(f"오류: 영상 파일이 없습니다: {args.video}", file=sys.stderr)
        return 1

    ctx = PipelineContext(
        video_path=args.video.resolve(),
        out_root=(args.out / args.video.stem).resolve(),
        force=args.force,
        whisper_model=args.whisper_model,
        language=args.language,
        scene_threshold=args.scene_threshold,
    )
    summary = run_pipeline(ctx)
    return 0 if summary["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
