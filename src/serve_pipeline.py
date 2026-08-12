"""Serve the local Pipeline Application Service through REST API v1."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from pipeline.deployment import embedding_deployments_from_environment
from video_preprocess.api import PipelineHTTPServer
from video_preprocess.engine import DAGPlanner, create_default_registry
from video_preprocess.services import (
    LocalMediaCatalog,
    LocalPipelineRunQueryResolver,
    LocalPipelineRuntimeFactory,
    LocalPipelineRunRepository,
    PipelineApplicationService,
    PipelineRunService,
    QueryService,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Local Video Preprocess Pipeline REST API v1 server"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--media-root", type=Path, default=Path("samples"))
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=Path("output/api-runs"),
    )
    parser.add_argument(
        "--state-root",
        type=Path,
        default=Path("output/api-state"),
    )
    parser.add_argument(
        "--auth-token-env",
        default=None,
        help="API bearer token을 읽을 환경변수 이름",
    )
    parser.add_argument("--max-active-runs", type=int, default=1)
    parser.add_argument("--retain-terminal-runs", type=int, default=1000)
    parser.add_argument("--max-request-bytes", type=int, default=1024 * 1024)
    parser.add_argument("--embedding-endpoint", default=None)
    parser.add_argument("--embedding-token-env", default=None)
    parser.add_argument(
        "--embedding-artifact-namespace",
        action="append",
        default=[],
    )
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[1]
    try:
        token = _token_from_environment(args.auth_token_env, os.environ)
        deployments = embedding_deployments_from_environment(
            endpoint=args.embedding_endpoint,
            token_env=args.embedding_token_env,
            artifact_namespaces=args.embedding_artifact_namespace,
            environ=os.environ,
        )
        application = PipelineApplicationService(
            DAGPlanner(create_default_registry()),
            LocalPipelineRuntimeFactory(project_root=project_root),
        )
        run_service = PipelineRunService(
            application,
            LocalPipelineRunRepository(
                args.state_root,
                retain_terminal_runs=args.retain_terminal_runs,
            ),
            LocalMediaCatalog(args.media_root),
            args.workspace_root,
            deployments=deployments,
            max_active_runs=args.max_active_runs,
        )
        query_service = QueryService(
            LocalPipelineRunQueryResolver(
                run_service,
                args.workspace_root,
            ),
            deployments=deployments,
        )
        server = PipelineHTTPServer(
            run_service=run_service,
            query_service=query_service,
            host=args.host,
            port=args.port,
            auth_token=token,
            max_request_bytes=args.max_request_bytes,
        )
    except (TypeError, ValueError, OSError, RuntimeError) as exc:
        print(
            f"오류: pipeline server를 시작할 수 없습니다: {exc}",
            file=sys.stderr,
        )
        return 2

    print(
        "Pipeline server 시작: "
        f"{server.base_url} media_root={args.media_root}"
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
