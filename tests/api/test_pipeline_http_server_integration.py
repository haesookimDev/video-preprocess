"""Loopback integration tests for the Pipeline REST API adapter."""

import asyncio
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from video_preprocess.api import PipelineHTTPServer
from video_preprocess.domain import RunStatus
from video_preprocess.engine import PipelineRunResult
from video_preprocess.services import (
    LocalMediaCatalog,
    LocalPipelineRunRepository,
    PipelineQueryResult,
    PipelineRunService,
)


pytestmark = pytest.mark.integration


class Application:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def plan(self, request):
        return SimpleNamespace(stage_names=())

    async def run(self, request, *, cancellation=None):
        self.started.set()
        while not self.release.is_set() and not cancellation.cancelled:
            await asyncio.sleep(0.001)
        return PipelineRunResult(
            run_id=request.run_id,
            status=(
                RunStatus.CANCELLED
                if cancellation.cancelled
                else RunStatus.SUCCEEDED
            ),
            stages=(),
            artifacts={},
            transitions=(),
        )


class QueryApplication:
    async def query(self, request):
        return PipelineQueryResult(
            run_id=request.run_id,
            query=request.query,
            context="loopback context",
            matches=(),
            context_stats={
                "tokenizer_model": "fake",
                "max_tokens": 4096,
                "token_count": 2,
                "requested_scene_ids": [],
                "expanded_scene_ids": [],
                "included_scene_ids": [],
                "excluded_scene_ids": [],
                "truncated_scene_ids": [],
            },
        )


def request_json(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, object] | None = None,
    token: str | None = "secret-token",
    idempotency_key: str | None = None,
) -> tuple[int, dict[str, object], dict[str, str]]:
    data = None if body is None else json.dumps(body).encode()
    headers = {}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method,
    )
    try:
        response = urllib.request.urlopen(request, timeout=2)
    except urllib.error.HTTPError as exc:
        response = exc
    payload = json.loads(response.read().decode())
    return response.status, payload, dict(response.headers.items())


def make_server(tmp_path: Path, application: Application):
    media = tmp_path / "media"
    media.mkdir()
    (media / "sample.mp4").write_bytes(b"video")
    service = PipelineRunService(
        application,
        LocalPipelineRunRepository(tmp_path / "state"),
        LocalMediaCatalog(media),
        tmp_path / "runs",
        run_id_factory=lambda: "run_http_test",
    )
    return PipelineHTTPServer(
        run_service=service,
        query_service=QueryApplication(),
        host="127.0.0.1",
        port=0,
        auth_token="secret-token",
        max_request_bytes=4096,
    )


def test_loopback_create_idempotency_status_artifacts_and_auth(
    tmp_path: Path,
) -> None:
    application = Application()
    body = {
        "schema_version": "1",
        "idempotency_key": "idem-http",
        "media_id": "sample.mp4",
    }
    with make_server(tmp_path, application) as server:
        unauthorized, error, _ = request_json(
            f"{server.base_url}/v1/pipeline-runs/missing",
            token=None,
        )
        accepted, run, headers = request_json(
            f"{server.base_url}/v1/pipeline-runs",
            method="POST",
            body=body,
            idempotency_key="idem-http",
        )
        recovered, same_run, _ = request_json(
            f"{server.base_url}/v1/pipeline-runs",
            method="POST",
            body=body,
            idempotency_key="idem-http",
        )
        not_ready, pending_error, _ = request_json(
            f"{server.base_url}/v1/pipeline-runs/run_http_test/artifacts"
        )
        application.release.set()
        deadline = time.monotonic() + 2
        while True:
            _, terminal, _ = request_json(
                f"{server.base_url}/v1/pipeline-runs/run_http_test"
            )
            if terminal["status"] == "succeeded":
                break
            if time.monotonic() > deadline:
                raise AssertionError("pipeline run did not finish")
        artifact_status, artifacts, _ = request_json(
            f"{server.base_url}/v1/pipeline-runs/run_http_test/artifacts"
        )
        query_status, query_result, _ = request_json(
            f"{server.base_url}/v1/pipeline-runs/run_http_test/queries",
            method="POST",
            body={"schema_version": "1", "query": "질의", "top_k": 2},
        )

    assert unauthorized == 401
    assert error["error"]["code"] == "UNAUTHORIZED"
    assert accepted == 202
    assert headers["Location"].endswith("/run_http_test")
    assert recovered == 200
    assert same_run["run_id"] == run["run_id"]
    assert not_ready == 409
    assert pending_error["error"]["code"] == "RUN_NOT_READY"
    assert artifact_status == 200
    assert artifacts["artifacts"] == {}
    assert query_status == 200
    assert query_result["context"] == "loopback context"


def test_loopback_rejects_mismatched_key_and_oversized_body(
    tmp_path: Path,
) -> None:
    application = Application()
    with make_server(tmp_path, application) as server:
        mismatch, mismatch_error, _ = request_json(
            f"{server.base_url}/v1/pipeline-runs",
            method="POST",
            body={
                "schema_version": "1",
                "idempotency_key": "body-key",
                "media_id": "sample.mp4",
            },
            idempotency_key="header-key",
        )
        oversized, size_error, _ = request_json(
            f"{server.base_url}/v1/pipeline-runs",
            method="POST",
            body={
                "schema_version": "1",
                "idempotency_key": "large",
                "media_id": "sample.mp4",
                "padding": "x" * 5000,
            },
            idempotency_key="large",
        )

    assert mismatch == 400
    assert mismatch_error["error"]["code"] == "INVALID_REQUEST"
    assert oversized == 413
    assert size_error["error"]["code"] == "PAYLOAD_TOO_LARGE"
