"""Evaluate a completed pipeline index against a fixed retrieval dataset."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from pipeline.deployment import embedding_deployments_from_environment
from pipeline.logging_setup import setup_logging
from video_preprocess.inference import InferenceCallError
from video_preprocess.services import (
    FixedQueryTargetResolver,
    QueryService,
    QueryServiceError,
    evaluate_retrieval,
    load_evaluation_cases,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="검색 품질 평가")
    parser.add_argument("out_root", type=Path, help="파이프라인 출력 디렉토리")
    parser.add_argument("dataset", type=Path, help="versioned 평가 JSON")
    parser.add_argument("--topk", type=int, default=3)
    parser.add_argument("--min-similarity", type=float, default=0.35)
    parser.add_argument("--embedding-endpoint", default=None)
    parser.add_argument("--embedding-token-env", default=None)
    parser.add_argument(
        "--context-tokenizer-model",
        default=None,
        help="context token 계산용 tokenizer (기본: index model)",
    )
    args = parser.parse_args()

    try:
        cases = load_evaluation_cases(args.dataset)
        deployments = embedding_deployments_from_environment(
            endpoint=args.embedding_endpoint,
            token_env=args.embedding_token_env,
            artifact_namespaces=(),
            environ=os.environ,
        )
    except (OSError, TypeError, ValueError, QueryServiceError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2

    output_root = args.out_root.resolve()
    log_dir = output_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    evaluation_id = datetime.now().strftime("evaluation_%Y%m%d_%H%M%S")
    service = QueryService(
        FixedQueryTargetResolver(output_root),
        deployments=deployments,
        context_tokenizer_model=args.context_tokenizer_model,
        logger=setup_logging(log_dir / f"{evaluation_id}.log"),
    )
    try:
        report = asyncio.run(
            evaluate_retrieval(
                service,
                cases,
                run_id=evaluation_id,
                top_k=args.topk,
                min_similarity=args.min_similarity,
            )
        )
    except (InferenceCallError, QueryServiceError, TypeError, ValueError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
