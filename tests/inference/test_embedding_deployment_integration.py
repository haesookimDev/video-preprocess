"""Remote embedding deployment tests through composition and Stage layers."""

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from pipeline.context import PipelineContext
from pipeline.stages import s10_index
from video_preprocess.domain import StageTask
from video_preprocess.inference import (
    HTTPProviderSettings,
    InferenceDeploymentSettings,
    create_configured_embedding_service,
)
from video_preprocess.services import (
    LocalPipelineRuntimeFactory,
    PipelineRunRequest,
)

from tests.support.fake_inference_server import FakeInferenceServer


pytestmark = pytest.mark.integration


def deployments(endpoint: str) -> InferenceDeploymentSettings:
    return InferenceDeploymentSettings(
        http_providers={
            "embedding.default": HTTPProviderSettings(
                endpoint=endpoint,
                poll_interval_sec=0.001,
                max_poll_interval_sec=0.01,
            )
        }
    )


def test_index_stage_runs_unchanged_with_remote_embedding_service(
    tmp_path: Path,
) -> None:
    with FakeInferenceServer(alias="embedding.default") as server:
        context = PipelineContext(
            video_path=tmp_path / "sample.mp4",
            out_root=tmp_path / "output" / "sample",
            embed_model="example/embedding",
            embedding_service=create_configured_embedding_service(
                "example/embedding",
                deployments=deployments(server.base_url),
            ),
        )
        timeline_dir = context.stage_dir("09_timeline")
        (timeline_dir / "timeline.json").write_text(
            json.dumps(
                {
                    "scene_cards": [
                        {
                            "scene_id": 1,
                            "start_sec": 0.0,
                            "end_sec": 5.0,
                            "caption": "첫 장면",
                            "transcript": [{"text": "첫 내용"}],
                        },
                        {
                            "scene_id": 2,
                            "start_sec": 5.0,
                            "end_sec": 10.0,
                            "caption": "둘째 장면",
                            "transcript": [{"text": "둘째 내용"}],
                        },
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        result = s10_index.run(context)

    db = sqlite3.connect(context.out_root / "10_index" / "index.db")
    try:
        metadata = dict(db.execute("SELECT key, value FROM meta"))
    finally:
        db.close()
    assert result == {"card_count": 2, "embed_dim": 2}
    assert metadata["embed_provider"] == "http.embedding"
    assert metadata["embed_revision"] == "fake-commit-1"
    assert server.service.inference_count == 1


def test_remote_capability_fingerprint_reaches_engine_model_resolver(
    tmp_path: Path,
) -> None:
    video = tmp_path / "sample.mp4"
    video.write_bytes(b"video")
    with FakeInferenceServer(alias="embedding.default") as server:
        runtime = LocalPipelineRuntimeFactory().create_preview(
            PipelineRunRequest(
                video_path=video,
                output_root=tmp_path / "output",
                deployments=deployments(server.base_url),
            ),
            run_id="run-123",
            boundary_inputs=("video",),
        )
        task = StageTask(
            run_id="run-123",
            stage_run_id="stage-123",
            attempt=1,
            stage="10_index",
            stage_version="1.0.0",
            inputs={},
            config={},
            model_bindings={"embedding": "embedding.default"},
            idempotency_key="idem-123",
            trace_id="trace-123",
        )

        models = asyncio.run(runtime.engine.model_resolver.resolve(task))

    assert models is not None
    assert [model.to_dict() for model in models] == [
        {
            "slot": "embedding",
            "provider": "http.embedding",
            "model": "example/embedding",
            "revision": "fake-commit-1",
            "runtime": "fake-inference/1.0",
        }
    ]
