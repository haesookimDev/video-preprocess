"""Compatibility tests for the provider-backed VAD Stage."""

import json
from pathlib import Path

from pipeline.context import PipelineContext
from pipeline.stages import s05_vad
from video_preprocess.domain import EffectiveModel
from video_preprocess.inference import SpeechSegment, VADBatch
from video_preprocess.storage import LocalArtifactStore, LegacyOutputAdapter


class FakeVADService:
    def __init__(self) -> None:
        self.audio = None

    def detect(self, audio, **kwargs) -> VADBatch:
        self.audio = audio
        assert kwargs == {
            "min_silence_duration_ms": 500,
            "speech_pad_ms": 200,
            "sampling_rate": 16000,
            "run_id": "sample",
            "stage_run_id": "05_vad",
        }
        return VADBatch(
            segments=(
                SpeechSegment(
                    segment_id=1,
                    start_sec=0.5,
                    end_sec=1.0,
                    duration_sec=0.5,
                ),
            ),
            total_sec=3.0,
            speech_sec=0.5,
            speech_ratio=0.167,
            model=EffectiveModel(
                provider="fake.vad",
                name="silero-vad-v6",
                revision="sha256:model123",
                runtime="fake/1.0",
            ),
            usage={"sample_count": 48000, "segment_count": 1},
            timing={"inference_sec": 0.01},
        )


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_vad_stage_keeps_legacy_output_with_provider_metadata(
    tmp_path: Path,
) -> None:
    context = PipelineContext(
        video_path=tmp_path / "sample.mp4",
        out_root=tmp_path / "output" / "sample",
    )
    audio_dir = context.stage_dir("04_audio")
    (audio_dir / "audio_16k.wav").write_bytes(b"audio")
    write_json(
        audio_dir / "audio.json",
        {
            "has_audio": True,
            "path": "04_audio/audio_16k.wav",
            "sample_rate": 16000,
            "channels": 1,
            "duration_sec": 3.0,
            "size_bytes": 5,
        },
    )
    service = FakeVADService()
    store = LocalArtifactStore(context.out_root, namespace="sample")
    context.vad_service = service
    context.artifact_registrar = LegacyOutputAdapter(store)

    result = s05_vad.run(context)

    output = json.loads(
        (context.out_root / "05_vad" / "vad_segments.json").read_text(
            encoding="utf-8"
        )
    )
    assert result == {"segment_count": 1, "speech_ratio": 0.167}
    assert service.audio.uri.startswith("artifact://sample/")
    assert output["has_audio"] is True
    assert output["model"] == "silero-vad-v6"
    assert output["provider"] == "fake.vad"
    assert output["revision"] == "sha256:model123"
    assert output["runtime"] == "fake/1.0"
    assert output["options"] == {
        "min_silence_duration_ms": 500,
        "speech_pad_ms": 200,
    }
    assert output["segments"] == [
        {
            "segment_id": 1,
            "start_sec": 0.5,
            "end_sec": 1.0,
            "duration_sec": 0.5,
        }
    ]


def test_vad_stage_without_audio_keeps_skip_output(tmp_path: Path) -> None:
    context = PipelineContext(
        video_path=tmp_path / "sample.mp4",
        out_root=tmp_path / "output" / "sample",
    )
    audio_dir = context.stage_dir("04_audio")
    write_json(audio_dir / "audio.json", {"has_audio": False})

    result = s05_vad.run(context)

    output = json.loads(
        (context.out_root / "05_vad" / "vad_segments.json").read_text(
            encoding="utf-8"
        )
    )
    assert result == {"segment_count": 0}
    assert output == {"has_audio": False, "segments": []}
