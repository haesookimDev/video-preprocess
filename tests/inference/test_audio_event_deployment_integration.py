"""Remote audio-event deployment through composition and unchanged Stage."""

from pathlib import Path

import pytest

from pipeline.context import PipelineContext
from pipeline.stages import s05_audio_events
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
    create_configured_audio_event_service,
)
from video_preprocess.storage import LocalArtifactStore, LegacyOutputAdapter

from tests.support.fake_inference_server import FakeInferenceServer


pytestmark = pytest.mark.integration

MODEL = EffectiveModel(
    provider="http.audio-event",
    name="example/audio-event",
    revision="fake-audio-commit-1",
    runtime="fake-inference/1.0",
)


def _response(request: InferenceRequest) -> InferenceResponse:
    return InferenceResponse(
        request_id=request.request_id,
        status=InferenceStatus.SUCCEEDED,
        outputs={
            "results": [
                {
                    "window_id": window["window_id"],
                    "labels": [
                        {"label": "music", "confidence": 0.93}
                    ],
                }
                for window in request.inputs["windows"]
            ]
        },
        model=MODEL,
        timing={"inference_sec": 0.0},
    )


def test_audio_event_stage_runs_unchanged_with_remote_provider(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output" / "sample"
    store = LocalArtifactStore(output, namespace="sample")
    with FakeInferenceServer(
        alias="audio_event.default",
        responder=_response,
        task=InferenceTask.AUDIO_EVENT_DETECTION,
        effective_model=MODEL,
        input_media_types=("audio/wav",),
        features=("window_batch", "audio-events-v1"),
    ) as server:
        deployments = InferenceDeploymentSettings(http_providers={
            "audio_event.default": HTTPProviderSettings(
                endpoint=server.base_url,
                allowed_artifact_namespaces=("sample",),
                poll_interval_sec=0.001,
                max_poll_interval_sec=0.01,
            )
        })
        context = PipelineContext(
            video_path=tmp_path / "sample.mp4",
            out_root=output,
            audio_event_mode="all",
            audio_event_model="example/audio-event",
            audio_event_labels=("music",),
            audio_event_service=create_configured_audio_event_service(
                "example/audio-event",
                store,
                deployments=deployments,
                max_batch_size=2,
            ),
            artifact_registrar=LegacyOutputAdapter(store),
        )
        audio = context.stage_dir("04_audio") / "audio_16k.wav"
        audio.write_bytes(b"RIFF-audio")
        context.save_json(
            context.stage_dir("04_audio") / "audio.json",
            {
                "has_audio": True,
                "path": "04_audio/audio_16k.wav",
                "sample_rate": 16000,
                "channels": 1,
                "duration_sec": 6.0,
            },
        )

        metrics = s05_audio_events.run(context)

    payload = context.load_json(
        output / "05_audio_events" / "audio_events.json"
    )
    assert metrics == {"audio_event_count": 1}
    assert payload["provider"] == "http.audio-event"
    assert payload["revision"] == "fake-audio-commit-1"
    assert payload["events"][0]["label"] == "music"
    assert payload["events"][0]["source_window_ids"] == [1, 2, 3]
    assert server.service.inference_count == 2
