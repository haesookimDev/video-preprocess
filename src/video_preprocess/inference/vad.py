"""Task-specific adapter for voice activity detection responses."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import uuid
from collections.abc import Mapping
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
class SpeechSegment:
    """One normalized VAD speech segment on the source timeline."""

    segment_id: int
    start_sec: float
    end_sec: float
    duration_sec: float

    def __post_init__(self) -> None:
        if (
            isinstance(self.segment_id, bool)
            or not isinstance(self.segment_id, int)
            or self.segment_id < 1
        ):
            raise ValueError("segment_id must be a positive integer")
        start = _finite_number(self.start_sec, "start_sec")
        end = _finite_number(self.end_sec, "end_sec")
        duration = _finite_number(self.duration_sec, "duration_sec")
        if start < 0:
            raise ValueError("start_sec must not be negative")
        if end <= start:
            raise ValueError("end_sec must be greater than start_sec")
        if duration <= 0 or abs(duration - (end - start)) > 0.002:
            raise ValueError("duration_sec must match the segment timestamps")
        object.__setattr__(self, "start_sec", start)
        object.__setattr__(self, "end_sec", end)
        object.__setattr__(self, "duration_sec", duration)

    def to_dict(self) -> dict[str, object]:
        return {
            "segment_id": self.segment_id,
            "start_sec": self.start_sec,
            "end_sec": self.end_sec,
            "duration_sec": self.duration_sec,
        }

    @classmethod
    def from_value(cls, value: object, index: int) -> "SpeechSegment":
        if not isinstance(value, Mapping):
            raise ValueError(f"segments[{index}] must be an object")
        return cls(
            segment_id=value.get("segment_id"),
            start_sec=value.get("start_sec"),
            end_sec=value.get("end_sec"),
            duration_sec=value.get("duration_sec"),
        )


@dataclass(frozen=True, slots=True)
class VADBatch:
    """Validated VAD output and effective model metadata."""

    segments: tuple[SpeechSegment, ...]
    total_sec: float
    speech_sec: float
    speech_ratio: float
    model: EffectiveModel
    usage: dict[str, object]
    timing: dict[str, object]


class VADService:
    """Build VAD requests and validate provider outputs."""

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

    def detect(
        self,
        audio: ArtifactRef,
        *,
        min_silence_duration_ms: int = 500,
        speech_pad_ms: int = 200,
        sampling_rate: int = 16000,
        run_id: str = "compat_pipeline",
        stage_run_id: str = "voice_activity_detection",
        trace_id: str | None = None,
    ) -> VADBatch:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.detect_async(
                    audio,
                    min_silence_duration_ms=min_silence_duration_ms,
                    speech_pad_ms=speech_pad_ms,
                    sampling_rate=sampling_rate,
                    run_id=run_id,
                    stage_run_id=stage_run_id,
                    trace_id=trace_id,
                )
            )
        raise RuntimeError(
            "VADService.detect() cannot run inside an event loop; "
            "use await detect_async()"
        )

    async def detect_async(
        self,
        audio: ArtifactRef,
        *,
        min_silence_duration_ms: int = 500,
        speech_pad_ms: int = 200,
        sampling_rate: int = 16000,
        run_id: str = "compat_pipeline",
        stage_run_id: str = "voice_activity_detection",
        trace_id: str | None = None,
    ) -> VADBatch:
        if not isinstance(audio, ArtifactRef):
            raise ValueError("audio must be an ArtifactRef")
        min_silence_duration_ms = self._normalize_milliseconds(
            min_silence_duration_ms,
            "min_silence_duration_ms",
        )
        speech_pad_ms = self._normalize_milliseconds(
            speech_pad_ms,
            "speech_pad_ms",
        )
        if sampling_rate != 16000:
            raise ValueError("sampling_rate must be 16000")
        parameters = {
            "min_silence_duration_ms": min_silence_duration_ms,
            "speech_pad_ms": speech_pad_ms,
            "sampling_rate": sampling_rate,
        }
        request_id = f"infer_{uuid.uuid4().hex}"
        trace_id = trace_id or f"trace_{uuid.uuid4().hex}"
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "alias": self.alias,
                    "model": self.model_name,
                    "revision": self.revision,
                    "audio": audio.to_dict(),
                    "parameters": parameters,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        request = InferenceRequest(
            request_id=request_id,
            idempotency_key=f"vad_{fingerprint}",
            run_id=run_id,
            stage_run_id=stage_run_id,
            task=InferenceTask.VOICE_ACTIVITY_DETECTION,
            model=RequestedModel(
                alias=self.alias,
                name=self.model_name,
                revision=self.revision,
            ),
            inputs={"audio": audio},
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
                "successful VAD response has no model",
            )
        try:
            segments = self._normalize_segments(
                response.outputs.get("segments")
            )
            total_sec = _finite_number(
                response.outputs.get("total_sec"),
                "total_sec",
            )
            speech_sec = _finite_number(
                response.outputs.get("speech_sec"),
                "speech_sec",
            )
            speech_ratio = _finite_number(
                response.outputs.get("speech_ratio"),
                "speech_ratio",
            )
            self._validate_totals(
                segments,
                total_sec,
                speech_sec,
                speech_ratio,
            )
        except ValueError as exc:
            raise self._invalid_response(request_id, str(exc)) from exc
        return VADBatch(
            segments=segments,
            total_sec=total_sec,
            speech_sec=speech_sec,
            speech_ratio=speech_ratio,
            model=response.model,
            usage=dict(response.usage),
            timing=dict(response.timing),
        )

    @staticmethod
    def _normalize_milliseconds(value: int, field_name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{field_name} must be an integer")
        if value < 0 or value > 600_000:
            raise ValueError(f"{field_name} must be between 0 and 600000")
        return value

    @staticmethod
    def _normalize_segments(value: object) -> tuple[SpeechSegment, ...]:
        if not isinstance(value, list):
            raise ValueError("segments must be an array")
        segments = tuple(
            SpeechSegment.from_value(segment, index)
            for index, segment in enumerate(value)
        )
        previous_end = 0.0
        for index, segment in enumerate(segments):
            if segment.segment_id != index + 1:
                raise ValueError("segment_id values must be contiguous from 1")
            if segment.start_sec < previous_end:
                raise ValueError("segments must be ordered and non-overlapping")
            previous_end = segment.end_sec
        return segments

    @staticmethod
    def _validate_totals(
        segments: tuple[SpeechSegment, ...],
        total_sec: float,
        speech_sec: float,
        speech_ratio: float,
    ) -> None:
        if total_sec < 0 or speech_sec < 0 or speech_sec > total_sec + 0.002:
            raise ValueError("VAD duration totals are invalid")
        if speech_ratio < 0 or speech_ratio > 1:
            raise ValueError("speech_ratio must be between 0 and 1")
        if segments and segments[-1].end_sec > total_sec + 0.002:
            raise ValueError("VAD segment exceeds total audio duration")
        segment_speech_sec = sum(
            segment.duration_sec for segment in segments
        )
        tolerance = max(0.002 * len(segments), 0.002)
        if abs(segment_speech_sec - speech_sec) > tolerance:
            raise ValueError("speech_sec does not match segment durations")
        expected_ratio = speech_sec / total_sec if total_sec else 0.0
        if abs(expected_ratio - speech_ratio) > 0.002:
            raise ValueError("speech_ratio does not match duration totals")

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
