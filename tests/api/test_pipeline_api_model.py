"""Explicit sample-video E2E through the production Pipeline REST API."""

import json
import time
import urllib.request
from pathlib import Path

import pytest

from video_preprocess.api import PipelineHTTPServer
from video_preprocess.engine import DAGPlanner, create_default_registry
from video_preprocess.services import (
    LocalMediaCatalog,
    LocalPipelineRuntimeFactory,
    LocalPipelineRunQueryResolver,
    LocalPipelineRunRepository,
    PipelineApplicationService,
    PipelineRunService,
    PublicRunStatus,
    QueryService,
)


pytestmark = [pytest.mark.integration, pytest.mark.model]
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def request_json(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, object]]:
    data = None if body is None else json.dumps(body).encode()
    request_headers = {} if headers is None else dict(headers)
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url,
        data=data,
        headers=request_headers,
        method=method,
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.status, json.loads(response.read().decode())


def test_sample_pipeline_and_query_cross_public_http_boundary(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    workspace_root = tmp_path / "runs"
    application = PipelineApplicationService(
        DAGPlanner(create_default_registry()),
        LocalPipelineRuntimeFactory(project_root=PROJECT_ROOT),
    )
    repository = LocalPipelineRunRepository(state_root)
    run_service = PipelineRunService(
        application,
        repository,
        LocalMediaCatalog(PROJECT_ROOT / "samples"),
        workspace_root,
    )
    query_service = QueryService(
        LocalPipelineRunQueryResolver(run_service, workspace_root)
    )
    with PipelineHTTPServer(
        run_service=run_service,
        query_service=query_service,
        host="127.0.0.1",
        port=0,
    ) as server:
        status, accepted = request_json(
            f"{server.base_url}/v1/pipeline-runs",
            method="POST",
            body={
                "schema_version": "1",
                "idempotency_key": "sample-api-e2e",
                "media_id": "sample.mp4",
                "settings": {"language": "ko"},
            },
            headers={"Idempotency-Key": "sample-api-e2e"},
        )
        assert status == 202
        run_id = accepted["run_id"]
        deadline = time.monotonic() + 300
        while True:
            _, current = request_json(
                f"{server.base_url}/v1/pipeline-runs/{run_id}"
            )
            if current["status"] in {"succeeded", "failed", "cancelled"}:
                break
            if time.monotonic() > deadline:
                raise AssertionError("sample API pipeline did not finish")
            time.sleep(0.1)
        artifact_status, artifacts = request_json(
            f"{server.base_url}/v1/pipeline-runs/{run_id}/artifacts"
        )
        query_status, query = request_json(
            f"{server.base_url}/v1/pipeline-runs/{run_id}/queries",
            method="POST",
            body={
                "schema_version": "1",
                "query": "음성 구간 검출",
                "top_k": 2,
                "max_context_tokens": 256,
                "adjacent_scenes": 1,
            },
        )

    persisted = LocalPipelineRunRepository(state_root).load(run_id)
    assert current["status"] == "succeeded"
    assert current["progress"] == {
        "planned_stages": 11,
        "completed_stages": 11,
        "ratio": 1.0,
        "current_stage": None,
        "current_attempt": None,
    }
    assert artifact_status == 200
    assert "search_index" in artifacts["artifacts"]
    assert all(
        artifact["uri"].startswith("artifact://")
        for artifact in artifacts["artifacts"].values()
    )
    assert str(tmp_path) not in json.dumps(artifacts)
    assert query_status == 200
    assert query["matches"][0]["scene_id"] == 2
    assert "### 씬 02" in query["context"]
    assert query["context_stats"]["max_tokens"] == 256
    assert query["context_stats"]["token_count"] <= 256
    assert query["context_stats"]["included_scene_ids"]
    assert persisted is not None
    assert persisted.status is PublicRunStatus.SUCCEEDED
