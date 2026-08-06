"""Task-specific adapter for speech-to-text inference responses."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from video_preprocess.domain import (
    ArtifactRef,
    EffectiveModel,
    InferenceErrorCode,
    InferenceFailure,
    InferenceRequest,
    InferenceStatus,
    InferenceTask,
    RequestedModel,
)

from .errors import InferenceCallError
from .gateway import InferenceGateway


def _finite_number(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be finite")
    return number


@dataclass(frozen=True, slots=True)
class SpeechChunk:
    """One absolute VAD time range sent to an STT provider."""

    start_sec: float
    end_sec: float
    source_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        start = _finite_number(self.start_sec, "start_sec")
        end = _finite_number(self.end_sec, "end_sec")
        if start < 0:
            raise ValueError("start_sec must not be negative")
        if end <= start:
            raise ValueError("end_sec must be greater than start_sec")
        if not isinstance(self.source_ids, tuple) or not self.source_ids:
            raise ValueError("source_ids must be a non-empty integer tuple")
        if any(
            isinstance(source_id, bool)
            or not isinstance(source_id, int)
            or source_id < 1
            for source_id in self.source_ids
        ):
            raise ValueError("source_ids must contain positive integers")
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("source_ids must not contain duplicates")
        object.__setattr__(self, "start_sec", start)
        object.__setattr__(self, "end_sec", end)

    def to_dict(self) -> dict[str, object]:
        return {
            "start_sec": self.start_sec,
            "end_sec": self.end_sec,
            "source_ids": list(self.source_ids),
        }

    @classmethod
    def from_value(cls, value: object, index: int) -> "SpeechChunk":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise ValueError(f"chunks[{index}] must be an object")
        source_ids = value.get("source_ids")
        if (
            isinstance(source_ids, (str, bytes))
            or not isinstance(source_ids, Sequence)
        ):
            raise ValueError(
                f"chunks[{index}].source_ids must be an integer array"
            )
        return cls(
            start_sec=value.get("start_sec"),
            end_sec=value.get("end_sec"),
            source_ids=tuple(source_ids),
        )


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    """Normalized absolute-timeline STT segment."""

    start_sec: float
    end_sec: float
    text: str
    avg_logprob: float
    no_speech_prob: float
    vad_source_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        start = _finite_number(self.start_sec, "start_sec")
        end = _finite_number(self.end_sec, "end_sec")
        avg_logprob = _finite_number(self.avg_logprob, "avg_logprob")
        no_speech_prob = _finite_number(
            self.no_speech_prob,
            "no_speech_prob",
        )
        if start < 0 or end < start:
            raise ValueError("segment timestamps are invalid")
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("text must be a non-empty string")
        if no_speech_prob < 0 or no_speech_prob > 1:
            raise ValueError("no_speech_prob must be between 0 and 1")
        if not isinstance(self.vad_source_ids, tuple) or not self.vad_source_ids:
            raise ValueError("vad_source_ids must be a non-empty integer tuple")
        if any(
            isinstance(source_id, bool)
            or not isinstance(source_id, int)
            or source_id < 1
            for source_id in self.vad_source_ids
        ):
            raise ValueError("vad_source_ids must contain positive integers")
        object.__setattr__(self, "start_sec", start)
        object.__setattr__(self, "end_sec", end)
        object.__setattr__(self, "text", self.text.strip())
        object.__setattr__(self, "avg_logprob", avg_logprob)
        object.__setattr__(self, "no_speech_prob", no_speech_prob)

    def to_dict(self) -> dict[str, object]:
        return {
            "start_sec": self.start_sec,
            "end_sec": self.end_sec,
            "text": self.text,
            "avg_logprob": self.avg_logprob,
            "no_speech_prob": self.no_speech_prob,
            "vad_source_ids": list(self.vad_source_ids),
        }

    @classmethod
    def from_value(cls, value: object, index: int) -> "TranscriptSegment":
        if not isinstance(value, Mapping):
            raise ValueError(f"segments[{index}] must be an object")
        source_ids = value.get("vad_source_ids")
        if (
            isinstance(source_ids, (str, bytes))
            or not isinstance(source_ids, Sequence)
        ):
            raise ValueError(
                f"segments[{index}].vad_source_ids must be an integer array"
            )
        return cls(
            start_sec=value.get("start_sec"),
            end_sec=value.get("end_sec"),
            text=value.get("text"),
            avg_logprob=value.get("avg_logprob"),
            no_speech_prob=value.get("no_speech_prob"),
            vad_source_ids=tuple(source_ids),
        )


@dataclass(frozen=True, slots=True)
class TranscriptionBatch:
    """Validated STT output and effective model metadata."""

    segments: tuple[TranscriptSegment, ...]
    language: str | None
    language_probability: float | None
    model: EffectiveModel
    usage: dict[str, object]
    timing: dict[str, object]


class STTService:
    """Build speech-to-text requests and validate provider outputs."""

    def __init__(
        self,
        gateway: InferenceGateway,
        *,
        alias: str,
        model_name: str,
        revision: str,
        timeout_sec: float = 900.0,
    ) -> None:
        self.gateway = gateway
        self.alias = alias
        self.model_name = model_name
        self.revision = revision
        self.timeout_sec = timeout_sec

    def transcribe(
        self,
        audio: ArtifactRef,
        chunks: Sequence[SpeechChunk | Mapping[str, object]],
        *,
        language: str | None = None,
        beam_size: int = 5,
        sampling_rate: int = 16000,
        run_id: str = "compat_pipeline",
        stage_run_id: str = "speech_to_text",
        trace_id: str | None = None,
    ) -> TranscriptionBatch:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.transcribe_async(
                    audio,
                    chunks,
                    language=language,
                    beam_size=beam_size,
                    sampling_rate=sampling_rate,
                    run_id=run_id,
                    stage_run_id=stage_run_id,
                    trace_id=trace_id,
                )
            )
        raise RuntimeError(
            "STTService.transcribe() cannot run inside an event loop; "
            "use await transcribe_async()"
        )

    async def transcribe_async(
        self,
        audio: ArtifactRef,
        chunks: Sequence[SpeechChunk | Mapping[str, object]],
        *,
        language: str | None = None,
        beam_size: int = 5,
        sampling_rate: int = 16000,
        run_id: str = "compat_pipeline",
        stage_run_id: str = "speech_to_text",
        trace_id: str | None = None,
    ) -> TranscriptionBatch:
        if not isinstance(audio, ArtifactRef):
            raise ValueError("audio must be an ArtifactRef")
        normalized_chunks = self._normalize_chunks(chunks)
        language = self._normalize_language(language)
        beam_size = self._normalize_beam_size(beam_size)
        if sampling_rate != 16000:
            raise ValueError("sampling_rate must be 16000")
        request_id = f"infer_{uuid.uuid4().hex}"
        trace_id = trace_id or f"trace_{uuid.uuid4().hex}"
        parameters = {
            "language": language,
            "beam_size": beam_size,
            "sampling_rate": sampling_rate,
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "alias": self.alias,
                    "model": self.model_name,
                    "revision": self.revision,
                    "audio": audio.to_dict(),
                    "chunks": [chunk.to_dict() for chunk in normalized_chunks],
                    "parameters": parameters,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        request = InferenceRequest(
            request_id=request_id,
            idempotency_key=f"stt_{fingerprint}",
            run_id=run_id,
            stage_run_id=stage_run_id,
            task=InferenceTask.SPEECH_TO_TEXT,
            model=RequestedModel(
                alias=self.alias,
                name=self.model_name,
                revision=self.revision,
            ),
            inputs={
                "audio": audio,
                "chunks": [chunk.to_dict() for chunk in normalized_chunks],
            },
            parameters=parameters,
            timeout_sec=self.timeout_sec,
            trace_id=trace_id,
        )
        response = await self.gateway.infer(request)
        if response.status is not InferenceStatus.SUCCEEDED:
            failure = response.error or InferenceFailure(
                code=InferenceErrorCode.INFERENCE_FAILED,
                message="provider failed without an error object",
                retryable=False,
                request_id=request_id,
            )
            raise InferenceCallError(failure)
        if response.model is None:
            raise self._invalid_response(
                request_id,
                "successful STT response has no model",
            )
        try:
            segments = self._normalize_segments(
                response.outputs.get("segments"),
            )
            response_language = self._normalize_language(
                response.outputs.get("language")
            )
            language_probability = self._normalize_probability(
                response.outputs.get("language_probability"),
                "language_probability",
            )
        except ValueError as exc:
            raise self._invalid_response(request_id, str(exc)) from exc
        return TranscriptionBatch(
            segments=segments,
            language=response_language,
            language_probability=language_probability,
            model=response.model,
            usage=dict(response.usage),
            timing=dict(response.timing),
        )

    @staticmethod
    def _normalize_chunks(
        chunks: Sequence[SpeechChunk | Mapping[str, object]],
    ) -> tuple[SpeechChunk, ...]:
        if isinstance(chunks, (str, bytes)) or not isinstance(chunks, Sequence):
            raise ValueError("chunks must be a sequence")
        normalized = tuple(
            SpeechChunk.from_value(chunk, index)
            for index, chunk in enumerate(chunks)
        )
        if not normalized:
            raise ValueError("chunks must not be empty")
        previous_end = 0.0
        for index, chunk in enumerate(normalized):
            if index > 0 and chunk.start_sec < previous_end:
                raise ValueError("chunks must be ordered and non-overlapping")
            previous_end = chunk.end_sec
        return normalized

    @staticmethod
    def _normalize_segments(value: object) -> tuple[TranscriptSegment, ...]:
        if not isinstance(value, list):
            raise ValueError("segments must be an array")
        segments = tuple(
            TranscriptSegment.from_value(segment, index)
            for index, segment in enumerate(value)
        )
        previous_start = 0.0
        for index, segment in enumerate(segments):
            if index > 0 and segment.start_sec < previous_start:
                raise ValueError("segments must be ordered by start_sec")
            previous_start = segment.start_sec
        return segments

    @staticmethod
    def _normalize_language(value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError("language must be a non-empty string or null")
        return value.strip()

    @staticmethod
    def _normalize_beam_size(value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("beam_size must be an integer")
        if value < 1 or value > 100:
            raise ValueError("beam_size must be between 1 and 100")
        return value

    @staticmethod
    def _normalize_probability(
        value: object,
        field_name: str,
    ) -> float | None:
        if value is None:
            return None
        probability = _finite_number(value, field_name)
        if probability < 0 or probability > 1:
            raise ValueError(f"{field_name} must be between 0 and 1")
        return probability

    @staticmethod
    def _invalid_response(
        request_id: str,
        message: str,
    ) -> InferenceCallError:
        return InferenceCallError(
            InferenceFailure(
                code=InferenceErrorCode.INFERENCE_FAILED,
                message=message,
                retryable=False,
                request_id=request_id,
            )
        )
