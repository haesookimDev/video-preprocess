"""Search a pipeline index and print assembled LLM context."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

from pipeline.deployment import embedding_deployments_from_environment
from pipeline.logging_setup import setup_logging
from video_preprocess.inference import InferenceCallError
from video_preprocess.services import (
    FixedQueryTargetResolver,
    PipelineQueryRequest,
    QueryService,
    QueryServiceError,
)
from video_preprocess.services.query import (
    assemble_context,
    embed_search,
    fts_search,
    rrf_fuse,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="씬 검색 + 컨텍스트 조립")
    parser.add_argument(
        "out_root",
        type=Path,
        help="파이프라인 출력 디렉토리 (예: output/sample)",
    )
    parser.add_argument("query", help="질의 문장")
    parser.add_argument("--topk", type=int, default=3)
    parser.add_argument(
        "--embedding-endpoint",
        default=None,
        help="embedding.default HTTP Inference v1 endpoint",
    )
    parser.add_argument(
        "--embedding-token-env",
        default=None,
        help="HTTP bearer token을 읽을 환경변수 이름",
    )
    args = parser.parse_args()

    try:
        deployments = embedding_deployments_from_environment(
            endpoint=args.embedding_endpoint,
            token_env=args.embedding_token_env,
            artifact_namespaces=(),
            environ=os.environ,
        )
        request = PipelineQueryRequest(
            run_id=datetime.now().strftime("%Y%m%d_%H%M%S"),
            query=args.query,
            top_k=args.topk,
        )
    except (TypeError, ValueError, QueryServiceError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2

    output_root = args.out_root.resolve()
    log_dir = output_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = setup_logging(log_dir / f"query_{request.run_id}.log")
    service = QueryService(
        FixedQueryTargetResolver(output_root),
        deployments=deployments,
        logger=log,
    )
    try:
        result = asyncio.run(service.query(request))
    except (QueryServiceError, InferenceCallError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1

    print("\n" + "=" * 60)
    print("조립된 LLM 입력 컨텍스트")
    print("=" * 60)
    print(result.context)
    return 0


if __name__ == "__main__":
    sys.exit(main())
