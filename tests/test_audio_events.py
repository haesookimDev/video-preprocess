"""Compatibility tests for the optional audio-event Stage."""

from pathlib import Path

from pipeline.context import PipelineContext
from pipeline.stages import s05_audio_events
from video_preprocess.domain import EffectiveModel
from video_preprocess.inference import AudioEvent, AudioEventBatch
from video_preprocess.storage import LocalArtifactStore, LegacyOutputAdapter


class FakeAudioEventService:
    def __init__(self) -> None:
        self.audio = None
        self.options = {}

    def detect(self, audio, **options) -> AudioEventBatch:
        self.audio = audio
        self.options = options
        return AudioEventBatch(
            events=(
                AudioEvent(
                    event_id=1,
                    label="music",
                    confidence=0.92,
                    start_sec=0.0,
                    end_sec=5.0,
                    source_window_ids=(1, 2),
                ),
            ),
            model=EffectiveModel(
                provider="fake.audio-event",
                name="example/audio-event",
                revision="commit-1",
                runtime="fake/1.0",
            ),
            usage={"window_count": 2},
            timing={"inference_sec": 0.01},
        )


def _context(tmp_path: Path, *, mode: str, has_audio: bool = True):
    context = PipelineContext(
        video_path=tmp_path / "sample.mp4",
        out_root=tmp_path / "output" / "sample",
        audio_event_mode=mode,
        audio_event_model="example/audio-event",
        audio_event_labels=("music", "applause"),
        audio_event_min_confidence=0.7,
        audio_event_window_sec=4.0,
        audio_event_hop_sec=2.0,
    )
    audio_path = context.stage_dir("04_audio") / "audio_16k.wav"
    if has_audio:
        audio_path.write_bytes(b"RIFF-audio")
    context.save_json(
        context.stage_dir("04_audio") / "audio.json",
        {
            "has_audio": has_audio,
            "path": "04_audio/audio_16k.wav" if has_audio else None,
            "sample_rate": 16000,
            "channels": 1,
            "duration_sec": 8.0,
        },
    )
    return context


def test_disabled_stage_writes_stable_sentinel_without_provider(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path, mode="disabled")

    metrics = s05_audio_events.run(context)

    payload = context.load_json(
        context.out_root / "05_audio_events" / "audio_events.json"
    )
    assert metrics == {
        "audio_event_count": 0,
        "skipped": "AUDIO_EVENTS_DISABLED",
    }
    assert payload["enabled"] is False
    assert payload["executed"] is False
    assert payload["reason_code"] == "AUDIO_EVENTS_DISABLED"
    assert payload["events"] == []


def test_enabled_stage_publishes_events_and_effective_model(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path, mode="all")
    service = FakeAudioEventService()
    store = LocalArtifactStore(context.out_root, namespace="sample")
    context.audio_event_service = service
    context.artifact_registrar = LegacyOutputAdapter(store)

    metrics = s05_audio_events.run(context)

    payload = context.load_json(
        context.out_root / "05_audio_events" / "audio_events.json"
    )
    assert metrics == {"audio_event_count": 1}
    assert service.audio.uri.startswith("artifact://sample/")
    assert service.options["duration_sec"] == 8.0
    assert service.options["labels"] == ("music", "applause")
    assert service.options["min_confidence"] == 0.7
    assert service.options["window_sec"] == 4.0
    assert service.options["hop_sec"] == 2.0
    assert payload["provider"] == "fake.audio-event"
    assert payload["revision"] == "commit-1"
    assert payload["events"][0] == {
        "event_id": 1,
        "label": "music",
        "confidence": 0.92,
        "start_sec": 0.0,
        "end_sec": 5.0,
        "duration_sec": 5.0,
        "source_window_ids": [1, 2],
    }


def test_enabled_stage_skips_no_audio_without_provider(tmp_path: Path) -> None:
    context = _context(tmp_path, mode="all", has_audio=False)

    metrics = s05_audio_events.run(context)

    payload = context.load_json(
        context.out_root / "05_audio_events" / "audio_events.json"
    )
    assert metrics["skipped"] == "NO_AUDIO"
    assert payload["enabled"] is True
    assert payload["reason_code"] == "NO_AUDIO"
