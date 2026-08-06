"""Compatibility tests for the provider-backed STT Stage."""

import json
from pathlib import Path

from pipeline.context import PipelineContext
from pipeline.stages import s06_stt
from video_preprocess.domain import EffectiveModel
from video_preprocess.inference import TranscriptSegment, TranscriptionBatch
from video_preprocess.storage import LocalArtifactStore, LegacyOutputAdapter


class FakeSTTService:
    def __init__(self) -> None:
        self.audio = None
        self.chunks = []

    def transcribe(self, audio, chunks, **kwargs) -> TranscriptionBatch:
        self.audio = audio
        self.chunks = list(chunks)
        assert kwargs["language"] is None
        assert kwargs["beam_size"] == 5
        assert kwargs["sampling_rate"] == 16000
        return TranscriptionBatch(
            segments=(
                TranscriptSegment(
                    start_sec=0.1,
                    end_sec=1.1,
                    text="첫 문장",
                    avg_logprob=-0.2,
                    no_speech_prob=0.01,
                    vad_source_ids=(1, 2),
                ),
            ),
            language="ko",
            language_probability=0.98,
            model=EffectiveModel(
                provider="fake.stt",
                name="base",
                revision="rev-1",
                runtime="fake/1.0",
            ),
            usage={"speech_duration_sec": 2.0},
            timing={"inference_sec": 0.5},
        )


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_stt_stage_keeps_legacy_output_with_provider_metadata(
    tmp_path: Path,
) -> None:
    context = PipelineContext(
        video_path=tmp_path / "sample.mp4",
        out_root=tmp_path / "output" / "sample",
        whisper_model="base",
    )
    audio_dir = context.stage_dir("04_audio")
    audio_path = audio_dir / "audio_16k.wav"
    audio_path.write_bytes(b"audio")
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
    vad_dir = context.stage_dir("05_vad")
    write_json(
        vad_dir / "vad_segments.json",
        {
            "segments": [
                {"segment_id": 1, "start_sec": 0.0, "end_sec": 1.0},
                {"segment_id": 2, "start_sec": 1.4, "end_sec": 2.0},
            ]
        },
    )
    service = FakeSTTService()
    store = LocalArtifactStore(context.out_root, namespace="sample")
    context.stt_service = service
    context.artifact_registrar = LegacyOutputAdapter(store)

    result = s06_stt.run(context)

    output = json.loads(
        (context.out_root / "06_stt" / "transcript.json").read_text(
            encoding="utf-8"
        )
    )
    assert result == {"transcript_count": 1, "language": "ko"}
    assert service.audio.uri.startswith("artifact://sample/")
    assert service.chunks == [
        {"start_sec": 0.0, "end_sec": 2.0, "source_ids": [1, 2]}
    ]
    assert output["segments"][0]["text"] == "첫 문장"
    assert output["model"] == "base"
    assert output["provider"] == "fake.stt"
    assert output["revision"] == "rev-1"
    assert output["runtime"] == "fake/1.0"
    assert output["language_probability"] == 0.98
    assert output["transcribe_elapsed_sec"] == 0.5


def test_stt_stage_with_no_speech_keeps_skip_output(tmp_path: Path) -> None:
    context = PipelineContext(
        video_path=tmp_path / "sample.mp4",
        out_root=tmp_path / "output" / "sample",
    )
    vad_dir = context.stage_dir("05_vad")
    write_json(vad_dir / "vad_segments.json", {"segments": []})

    result = s06_stt.run(context)

    output = json.loads(
        (context.out_root / "06_stt" / "transcript.json").read_text(
            encoding="utf-8"
        )
    )
    assert result == {"transcript_count": 0}
    assert output == {"segments": [], "language": None}
