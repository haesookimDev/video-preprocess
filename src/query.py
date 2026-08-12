"""Search a pipeline index and print assembled LLM context."""

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
        "--min-similarity",
        type=float,
        default=0.35,
        help="키워드 미일치 결과의 최소 cosine 유사도 (기본: 0.35)",
    )
    parser.add_argument(
        "--max-context-tokens",
        type=int,
        default=4096,
        help="조립 context 실제 token 상한 (기본: 4096)",
    )
    parser.add_argument(
        "--adjacent-scenes",
        type=int,
        default=1,
        help="각 검색 결과 앞뒤에 확장할 씬 수 (기본: 1)",
    )
    parser.add_argument(
        "--context-tokenizer-model",
        default=None,
        help="context token 계산용 tokenizer (기본: index embedding model)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="context 대신 점수·선택 근거를 포함한 JSON 출력",
    )
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
            min_similarity=args.min_similarity,
            max_context_tokens=args.max_context_tokens,
            adjacent_scenes=args.adjacent_scenes,
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
        context_tokenizer_model=args.context_tokenizer_model,
        logger=log,
    )
    try:
        result = asyncio.run(service.query(request))
    except (QueryServiceError, InferenceCallError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print("\n" + "=" * 60)
        print("조립된 LLM 입력 컨텍스트")
        print("=" * 60)
        print(result.context)
    return 0


if __name__ == "__main__":
    sys.exit(main())
