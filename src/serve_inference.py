"""Serve a local embedding model through HTTP Inference v1."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from video_preprocess.inference import InferenceHTTPServer
from video_preprocess.inference.local import LocalEmbeddingProvider


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Local embedding HTTP Inference v1 server"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--alias", default="embedding.default")
    parser.add_argument(
        "--model",
        default="paraphrase-multilingual-MiniLM-L12-v2",
    )
    parser.add_argument("--revision", default=None)
    parser.add_argument("--device", default=None)
    parser.add_argument(
        "--auth-token-env",
        default=None,
        help="bearer token을 읽을 환경변수 이름",
    )
    parser.add_argument("--max-jobs", type=int, default=1024)
    parser.add_argument(
        "--warmup",
        action="store_true",
        help="서버 bind 전에 embedding model을 로드",
    )
    args = parser.parse_args()

    try:
        token = _token_from_environment(args.auth_token_env, os.environ)
        provider = LocalEmbeddingProvider(
            alias=args.alias,
            model_name=args.model,
            revision=args.revision,
            device=args.device,
        )
        if args.warmup:
            asyncio.run(provider.warmup())
        server = InferenceHTTPServer(
            alias=args.alias,
            provider=provider,
            host=args.host,
            port=args.port,
            auth_token=token,
            max_jobs=args.max_jobs,
        )
    except (TypeError, ValueError, OSError, RuntimeError) as exc:
        print(f"오류: inference server를 시작할 수 없습니다: {exc}", file=sys.stderr)
        return 2

    print(
        "Inference server 시작: "
        f"{server.base_url} alias={args.alias} model={args.model}"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.close()
    return 0


def _token_from_environment(variable, environ) -> str | None:
    if variable is None:
        return None
    if not isinstance(variable, str) or not variable.strip():
        raise ValueError("auth_token_env must be non-empty")
    name = variable.strip()
    token = environ.get(name, "").strip()
    if not token:
        raise ValueError(f"token environment variable is empty: {name}")
    return token


if __name__ == "__main__":
    sys.exit(main())
