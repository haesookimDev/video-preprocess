"""Explicit real-model smoke test for the local AudioSet provider."""

import io
import wave
from pathlib import Path

import numpy as np
import pytest

from video_preprocess.inference.local import (
    DEFAULT_AUDIO_EVENT_MODEL,
    create_local_audio_event_service,
)
from video_preprocess.storage import LocalArtifactStore


pytestmark = [pytest.mark.integration, pytest.mark.model]


def _tone_wav(duration_sec: float = 1.0, sampling_rate: int = 16000) -> bytes:
    samples = (0.1 * np.sin(
        2 * np.pi * 440 * np.arange(int(duration_sec * sampling_rate))
        / sampling_rate
    ) * 32767).astype("<i2")
    stream = io.BytesIO()
    with wave.open(stream, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sampling_rate)
        wav.writeframes(samples.tobytes())
    return stream.getvalue()


def test_real_ast_model_classifies_one_pcm16_window(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path, namespace="model-test")
    pending = store.put(
        io.BytesIO(_tone_wav()),
        artifact_id="tone",
        relative_path="audio/tone.wav",
        kind="audio",
        media_type="audio/wav",
    )
    audio = store.publish(pending)
    service = create_local_audio_event_service(
        DEFAULT_AUDIO_EVENT_MODEL,
        store,
        device="cpu",
        max_batch_size=1,
    )

    result = service.detect(
        audio,
        duration_sec=1.0,
        labels=("music",),
        min_confidence=0.0,
        window_sec=1.0,
        hop_sec=1.0,
    )

    assert len(result.events) == 1
    assert result.events[0].label == "music"
    assert 0 <= result.events[0].confidence <= 1
    assert result.model.provider == "local.audio-event"
    assert result.model.name == DEFAULT_AUDIO_EVENT_MODEL
