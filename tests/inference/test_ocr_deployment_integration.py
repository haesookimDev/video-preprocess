"""Remote OCR deployment test through composition and the unchanged Stage."""

from pathlib import Path

import pytest

from pipeline.context import PipelineContext
from pipeline.stages import s08_ocr
from video_preprocess.domain import (
    EffectiveModel,
    InferenceRequest,
    InferenceResponse,
    InferenceStatus,
    InferenceTask,
)
from video_preprocess.inference import (
    HTTPProviderSettings,
    InferenceDeploymentSettings,
    create_configured_ocr_service,
)
from video_preprocess.storage import LocalArtifactStore, LegacyOutputAdapter

from tests.support.fake_inference_server import FakeInferenceServer


pytestmark = pytest.mark.integration


MODEL = EffectiveModel(
    provider="http.ocr",
    name="example/ocr",
    revision="fake-ocr-commit-1",
    runtime="fake-inference/1.0",
)


def _ocr_response(request: InferenceRequest) -> InferenceResponse:
    return InferenceResponse(
        request_id=request.request_id,
        status=InferenceStatus.SUCCEEDED,
        outputs={
            "results": [
                {
                    "artifact_id": image.artifact_id,
                    "text": "OPENAI",
                    "image_width": 640,
                    "image_height": 360,
                    "regions": [
                        {
                            "region_id": 1,
                            "text": "OPENAI",
                            "confidence": 0.99,
                            "bbox": {
                                "x": 10,
                                "y": 20,
                                "width": 100,
                                "height": 30,
                            },
                        }
                    ],
                }
                for image in request.inputs["images"]
            ]
        },
        model=MODEL,
        usage={"image_count": len(request.inputs["images"])},
        timing={"inference_sec": 0.0},
    )


def test_ocr_stage_runs_unchanged_with_remote_provider(tmp_path: Path) -> None:
    output = tmp_path / "output" / "sample"
    store = LocalArtifactStore(output, namespace="sample")
    with FakeInferenceServer(
        alias="ocr.default",
        responder=_ocr_response,
        task=InferenceTask.OPTICAL_CHARACTER_RECOGNITION,
        effective_model=MODEL,
        input_media_types=("image/jpeg",),
        features=("ordered_results", "word_boxes", "confidence"),
    ) as server:
        deployments = InferenceDeploymentSettings(
            http_providers={
                "ocr.default": HTTPProviderSettings(
                    endpoint=server.base_url,
                    allowed_artifact_namespaces=("sample",),
                    poll_interval_sec=0.001,
                    max_poll_interval_sec=0.01,
                )
            }
        )
        context = PipelineContext(
            video_path=tmp_path / "sample.mp4",
            out_root=output,
            ocr_mode="all",
            ocr_model="example/ocr",
            ocr_service=create_configured_ocr_service(
                "example/ocr",
                store,
                deployments=deployments,
            ),
            artifact_registrar=LegacyOutputAdapter(store),
        )
        frame = context.stage_dir("03_keyframes") / "frames" / "scene_001.jpg"
        frame.parent.mkdir(parents=True, exist_ok=True)
        frame.write_bytes(b"image")
        context.save_json(
            context.stage_dir("03_keyframes") / "keyframes.json",
            {
                "keyframes": [
                    {
                        "scene_id": 1,
                        "timestamp_sec": 1.0,
                        "path": "03_keyframes/frames/scene_001.jpg",
                    }
                ]
            },
        )

        metrics = s08_ocr.run(context)

    payload = context.load_json(output / "08_ocr" / "ocr.json")
    assert metrics["ocr_image_count"] == 1
    assert payload["provider"] == "http.ocr"
    assert payload["revision"] == "fake-ocr-commit-1"
    assert payload["results"][0]["text"] == "OPENAI"
    assert server.service.inference_count == 1
