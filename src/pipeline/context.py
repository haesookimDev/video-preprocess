"""파이프라인 전역 컨텍스트: 경로·설정을 모든 단계가 공유한다."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from video_preprocess.tokenization import TokenCounter
    from video_preprocess.inference import (
        CaptionService,
        DiarizationService,
        EmbeddingService,
        STTService,
        VADService,
    )
    from video_preprocess.storage import LegacyArtifactRegistrar


@dataclass
class PipelineContext:
    video_path: Path
    out_root: Path  # output/<video_stem>/
    force: bool = False

    # 단계별 설정
    scene_threshold: float = 27.0  # ContentDetector 임계값
    min_scene_len_frames: int = 15
    keyframes_per_scene: int = 1
    vad_min_silence_ms: int = 500
    vad_speech_pad_ms: int = 200
    stt_merge_gap_sec: float = 0.5  # VAD 세그먼트 병합 최대 간격
    whisper_model: str = "base"
    language: str | None = None  # None이면 자동 감지
    caption_model: str = "Salesforce/blip-image-captioning-base"
    embed_model: str = "paraphrase-multilingual-MiniLM-L12-v2"
    diarize_model: str = "pyannote/speaker-diarization-community-1"
    max_context_tokens: int | None = None
    context_tokenizer_model: str | None = None

    caption_service: CaptionService | None = field(
        default=None,
        repr=False,
    )
    stt_service: STTService | None = field(
        default=None,
        repr=False,
    )
    diarization_service: DiarizationService | None = field(
        default=None,
        repr=False,
    )
    vad_service: VADService | None = field(
        default=None,
        repr=False,
    )
    embedding_service: EmbeddingService | None = field(
        default=None,
        repr=False,
    )
    artifact_registrar: LegacyArtifactRegistrar | None = field(
        default=None,
        repr=False,
    )
    context_token_counter: TokenCounter | None = field(
        default=None,
        repr=False,
    )

    _created: bool = field(default=False, repr=False)

    def stage_dir(self, name: str) -> Path:
        d = self.out_root / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def log_dir(self) -> Path:
        d = self.out_root / "logs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save_json(self, path: Path, data) -> None:
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def load_json(self, path: Path):
        return json.loads(path.read_text(encoding="utf-8"))
