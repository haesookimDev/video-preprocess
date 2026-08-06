"""Compatibility tests for the provider-backed diarization Stage."""

import json
from pathlib import Path

import pytest

from pipeline.context import PipelineContext
from pipeline.stages import s07_diarize
from video_preprocess.domain import (
    EffectiveModel,
    InferenceErrorCode,
    InferenceFailure,
)
from video_preprocess.inference import (
    DiarizationBatch,
    InferenceCallError,
    SpeakerTurn,
)
from video_preprocess.storage import LocalArtifactStore, LegacyOutputAdapter


class FakeDiarizationService:
    def __init__(self, failure=None) -> None:
        self.audio = None
        self.failure = failure

    def diarize(self, audio, **kwargs) -> DiarizationBatch:
        self.audio = audio
        assert kwargs["run_id"] == "sample"
        assert kwargs["stage_run_id"] == "07_diarize"
        if self.failure is not None:
            raise InferenceCallError(self.failure)
        return DiarizationBatch(
            speakers=("SPEAKER_00",),
            turns=(
                SpeakerTurn(
                    turn_id=1,
                    start_sec=0.1,
                    end_sec=1.2,
                    speaker="SPEAKER_00",
                ),
            ),
            model=EffectiveModel(
                provider="fake.diarization",
                name="pyannote/test",
                revision="rev-1",
                runtime="fake/1.0",
            ),
            usage={"speaker_count": 1, "turn_count": 1},
            timing={"inference_sec": 0.5},
        )


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def make_context(tmp_path: Path) -> PipelineContext:
    context = PipelineContext(
        video_path=tmp_path / "sample.mp4",
        out_root=tmp_path / "output" / "sample",
        diarize_model="pyannote/test",
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
    store = LocalArtifactStore(context.out_root, namespace="sample")
    context.artifact_registrar = LegacyOutputAdapter(store)
    return context


def test_diarization_stage_keeps_legacy_output_with_provider_metadata(
    tmp_path: Path,
) -> None:
    context = make_context(tmp_path)
    service = FakeDiarizationService()
    context.diarization_service = service

    result = s07_diarize.run(context)

    output = json.loads(
        (context.out_root / "07_diarize" / "diarization.json").read_text(
            encoding="utf-8"
        )
    )
    assert result == {"speaker_count": 1, "turn_count": 1}
    assert service.audio.uri.startswith("artifact://sample/")
    assert output["available"] is True
    assert output["model"] == "pyannote/test"
    assert output["provider"] == "fake.diarization"
    assert output["revision"] == "rev-1"
    assert output["runtime"] == "fake/1.0"
    assert output["speakers"] == ["SPEAKER_00"]
    assert output["turns"][0]["speaker"] == "SPEAKER_00"


@pytest.mark.parametrize(
    ("code", "details", "reason_fragment"),
    [
        (
            InferenceErrorCode.AUTHENTICATION_FAILED,
            {"reason": "CREDENTIAL_MISSING"},
            "HF_TOKEN 없음",
        ),
        (
            InferenceErrorCode.AUTHENTICATION_FAILED,
            {},
            "HF_TOKEN 인증 실패",
        ),
        (
            InferenceErrorCode.MODEL_ACCESS_DENIED,
            {},
            "게이트 모델 접근 거부",
        ),
        (
            InferenceErrorCode.MODEL_UNAVAILABLE,
            {},
            "모델 로드 실패",
        ),
    ],
)
def test_diarization_stage_maps_expected_provider_failures_to_skip(
    tmp_path: Path,
    code: InferenceErrorCode,
    details: dict[str, object],
    reason_fragment: str,
) -> None:
    context = make_context(tmp_path)
    failure = InferenceFailure(
        code=code,
        message="provider failure",
        retryable=False,
        details=details,
        request_id="infer_test",
    )
    context.diarization_service = FakeDiarizationService(failure)

    result = s07_diarize.run(context)

    output = json.loads(
        (context.out_root / "07_diarize" / "diarization.json").read_text(
            encoding="utf-8"
        )
    )
    assert result["speaker_count"] == 0
    assert reason_fragment in result["skipped"]
    assert output == {
        "available": False,
        "reason": result["skipped"],
        "turns": [],
    }


def test_diarization_stage_reraises_unexpected_provider_failure(
    tmp_path: Path,
) -> None:
    context = make_context(tmp_path)
    failure = InferenceFailure(
        code=InferenceErrorCode.INFERENCE_FAILED,
        message="inference failed",
        retryable=False,
        request_id="infer_test",
    )
    context.diarization_service = FakeDiarizationService(failure)

    with pytest.raises(InferenceCallError):
        s07_diarize.run(context)


def test_diarization_stage_without_audio_keeps_skip_output(
    tmp_path: Path,
) -> None:
    context = PipelineContext(
        video_path=tmp_path / "sample.mp4",
        out_root=tmp_path / "output" / "sample",
    )
    audio_dir = context.stage_dir("04_audio")
    write_json(audio_dir / "audio.json", {"has_audio": False})

    result = s07_diarize.run(context)

    output = json.loads(
        (context.out_root / "07_diarize" / "diarization.json").read_text(
            encoding="utf-8"
        )
    )
    assert result == {"speaker_count": 0, "skipped": "오디오 없음"}
    assert output == {
        "available": False,
        "reason": "오디오 없음",
        "turns": [],
    }
