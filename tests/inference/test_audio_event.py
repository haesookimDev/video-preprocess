"""Contract tests for the windowed audio-event service."""

import asyncio

import pytest

from video_preprocess.domain import (
    ArtifactRef,
    Checksum,
    EffectiveModel,
    HealthState,
    InferenceRequest,
    InferenceResponse,
    InferenceStatus,
    InferenceTask,
    ProviderCapabilities,
    ProviderHealth,
)
from video_preprocess.inference import (
    AUDIO_EVENT_OVERLAP_POLICY,
    AUDIO_EVENT_TAXONOMY_VERSION,
    AudioEventService,
    InferenceCallError,
    InferenceGateway,
)


def _audio() -> ArtifactRef:
    return ArtifactRef(
        artifact_id="audio_16k",
        kind="audio",
        uri="artifact://sample/04_audio/audio_16k.wav",
        media_type="audio/wav",
        size_bytes=32000,
        checksum=Checksum("sha256", "abc123"),
    )


class FakeAudioEventProvider:
    def __init__(self, *, invalid_label: bool = False) -> None:
        self.invalid_label = invalid_label
        self.requests: list[InferenceRequest] = []

    async def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider="fake.audio-event",
            tasks=(InferenceTask.AUDIO_EVENT_DETECTION,),
            model_aliases=("audio_event.default",),
            input_media_types=("audio/wav",),
            features=(
                "window_batch",
                AUDIO_EVENT_TAXONOMY_VERSION,
                AUDIO_EVENT_OVERLAP_POLICY,
            ),
            max_batch_size=2,
        )

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        self.requests.append(request)
        results = []
        for window in request.inputs["windows"]:
            window_id = window["window_id"]
            labels = []
            if self.invalid_label:
                labels.append({"label": "unknown", "confidence": 0.9})
            else:
                if window_id in {1, 2}:
                    labels.append({
                        "label": "music",
                        "confidence": 0.8 + window_id * 0.05,
                    })
                if window_id == 2:
                    labels.append({"label": "applause", "confidence": 0.95})
                if window_id == 3:
                    labels.append({"label": "noise", "confidence": 0.49})
            results.append({"window_id": window_id, "labels": labels})
        return InferenceResponse(
            request_id=request.request_id,
            status=InferenceStatus.SUCCEEDED,
            outputs={"results": results},
            model=EffectiveModel(
                provider="fake.audio-event",
                name="example/audio-event",
                revision="commit-1",
                runtime="fake/1.0",
            ),
            timing={"inference_sec": 0.01},
        )

    async def cancel(self, request_id: str) -> None:
        return None

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider="fake.audio-event",
            status=HealthState.AVAILABLE,
        )


def _service(provider: FakeAudioEventProvider) -> AudioEventService:
    return AudioEventService(
        InferenceGateway({"audio_event.default": provider}),
        alias="audio_event.default",
        model_name="example/audio-event",
        revision="main",
        batch_size=8,
    )


def test_service_chunks_windows_and_merges_only_equal_labels() -> None:
    provider = FakeAudioEventProvider()

    batch = _service(provider).detect(
        _audio(),
        duration_sec=6,
        labels=("music", "applause", "noise"),
        min_confidence=0.5,
        window_sec=3,
        hop_sec=2,
        sampling_rate=16000,
        run_id="run-1",
        stage_run_id="05_audio_events",
        trace_id="trace-1",
    )

    assert [event.to_dict() for event in batch.events] == [
        {
            "event_id": 1,
            "label": "music",
            "confidence": 0.9,
            "start_sec": 0.0,
            "end_sec": 5.0,
            "duration_sec": 5.0,
            "source_window_ids": [1, 2],
        },
        {
            "event_id": 2,
            "label": "applause",
            "confidence": 0.95,
            "start_sec": 2.0,
            "end_sec": 5.0,
            "duration_sec": 3.0,
            "source_window_ids": [2],
        },
    ]
    assert batch.usage["batch_sizes"] == [2, 1]
    assert batch.usage["window_count"] == 3
    assert batch.timing["inference_sec"] == 0.02
    assert batch.model.revision == "commit-1"
    assert [request.task for request in provider.requests] == [
        InferenceTask.AUDIO_EVENT_DETECTION,
        InferenceTask.AUDIO_EVENT_DETECTION,
    ]
    assert provider.requests[0].inputs["audio"] == _audio()
    assert provider.requests[0].parameters == {
        "taxonomy_version": AUDIO_EVENT_TAXONOMY_VERSION,
        "labels": ["music", "applause", "noise"],
        "min_confidence": 0.5,
        "sampling_rate": 16000,
        "interval": "half-open",
    }
    assert provider.requests[0].idempotency_key != (
        provider.requests[1].idempotency_key
    )


def test_service_rejects_provider_label_outside_requested_taxonomy() -> None:
    with pytest.raises(InferenceCallError, match="invalid or duplicated"):
        _service(FakeAudioEventProvider(invalid_label=True)).detect(
            _audio(),
            duration_sec=1,
            labels=("music",),
        )


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"duration_sec": 0}, "duration_sec"),
        ({"duration_sec": 1, "labels": ()}, "labels"),
        ({"duration_sec": 1, "labels": ("speech",)}, "taxonomy"),
        ({"duration_sec": 1, "min_confidence": 1.1}, "min_confidence"),
        ({"duration_sec": 1, "window_sec": 1, "hop_sec": 2}, "hop_sec"),
    ],
)
def test_service_validates_public_options(options, message) -> None:
    with pytest.raises(ValueError, match=message):
        _service(FakeAudioEventProvider()).detect(_audio(), **options)


def test_sync_service_rejects_nested_event_loop() -> None:
    async def invoke() -> None:
        with pytest.raises(RuntimeError, match="detect_async"):
            _service(FakeAudioEventProvider()).detect(
                _audio(),
                duration_sec=1,
            )

    asyncio.run(invoke())
