"""Task-specific adapter for speaker diarization inference responses."""

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
class SpeakerTurn:
    """One normalized speaker turn on the source audio timeline."""

    turn_id: int
    start_sec: float
    end_sec: float
    speaker: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.turn_id, bool)
            or not isinstance(self.turn_id, int)
            or self.turn_id < 1
        ):
            raise ValueError("turn_id must be a positive integer")
        start = _finite_number(self.start_sec, "start_sec")
        end = _finite_number(self.end_sec, "end_sec")
        if start < 0:
            raise ValueError("start_sec must not be negative")
        if end <= start:
            raise ValueError("end_sec must be greater than start_sec")
        if not isinstance(self.speaker, str) or not self.speaker.strip():
            raise ValueError("speaker must be a non-empty string")
        object.__setattr__(self, "start_sec", start)
        object.__setattr__(self, "end_sec", end)
        object.__setattr__(self, "speaker", self.speaker.strip())

    def to_dict(self) -> dict[str, object]:
        return {
            "turn_id": self.turn_id,
            "start_sec": self.start_sec,
            "end_sec": self.end_sec,
            "speaker": self.speaker,
        }

    @classmethod
    def from_value(cls, value: object, index: int) -> "SpeakerTurn":
        if not isinstance(value, Mapping):
            raise ValueError(f"turns[{index}] must be an object")
        return cls(
            turn_id=value.get("turn_id"),
            start_sec=value.get("start_sec"),
            end_sec=value.get("end_sec"),
            speaker=value.get("speaker"),
        )


@dataclass(frozen=True, slots=True)
class DiarizationBatch:
    """Validated diarization output and effective model metadata."""

    speakers: tuple[str, ...]
    turns: tuple[SpeakerTurn, ...]
    model: EffectiveModel
    usage: dict[str, object]
    timing: dict[str, object]


class DiarizationService:
    """Build diarization requests and validate provider outputs."""

    def __init__(
        self,
        gateway: InferenceGateway,
        *,
        alias: str,
        model_name: str,
        revision: str,
        timeout_sec: float = 1800.0,
    ) -> None:
        self.gateway = gateway
        self.alias = alias
        self.model_name = model_name
        self.revision = revision
        self.timeout_sec = timeout_sec

    def diarize(
        self,
        audio: ArtifactRef,
        *,
        run_id: str = "compat_pipeline",
        stage_run_id: str = "speaker_diarization",
        trace_id: str | None = None,
    ) -> DiarizationBatch:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.diarize_async(
                    audio,
                    run_id=run_id,
                    stage_run_id=stage_run_id,
                    trace_id=trace_id,
                )
            )
        raise RuntimeError(
            "DiarizationService.diarize() cannot run inside an event loop; "
            "use await diarize_async()"
        )

    async def diarize_async(
        self,
        audio: ArtifactRef,
        *,
        run_id: str = "compat_pipeline",
        stage_run_id: str = "speaker_diarization",
        trace_id: str | None = None,
    ) -> DiarizationBatch:
        if not isinstance(audio, ArtifactRef):
            raise ValueError("audio must be an ArtifactRef")
        request_id = f"infer_{uuid.uuid4().hex}"
        trace_id = trace_id or f"trace_{uuid.uuid4().hex}"
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "alias": self.alias,
                    "model": self.model_name,
                    "revision": self.revision,
                    "audio": audio.to_dict(),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        request = InferenceRequest(
            request_id=request_id,
            idempotency_key=f"diarization_{fingerprint}",
            run_id=run_id,
            stage_run_id=stage_run_id,
            task=InferenceTask.SPEAKER_DIARIZATION,
            model=RequestedModel(
                alias=self.alias,
                name=self.model_name,
                revision=self.revision,
            ),
            inputs={"audio": audio},
            parameters={},
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
                "successful diarization response has no model",
            )
        try:
            speakers = self._normalize_speakers(
                response.outputs.get("speakers")
            )
            turns = self._normalize_turns(response.outputs.get("turns"))
            if tuple(sorted({turn.speaker for turn in turns})) != speakers:
                raise ValueError("speakers must match turn speaker labels")
        except ValueError as exc:
            raise self._invalid_response(request_id, str(exc)) from exc
        return DiarizationBatch(
            speakers=speakers,
            turns=turns,
            model=response.model,
            usage=dict(response.usage),
            timing=dict(response.timing),
        )

    @staticmethod
    def _normalize_speakers(value: object) -> tuple[str, ...]:
        if not isinstance(value, list):
            raise ValueError("speakers must be an array")
        speakers = []
        for index, speaker in enumerate(value):
            if not isinstance(speaker, str) or not speaker.strip():
                raise ValueError(
                    f"speakers[{index}] must be a non-empty string"
                )
            speakers.append(speaker.strip())
        if speakers != sorted(set(speakers)):
            raise ValueError("speakers must be sorted and unique")
        return tuple(speakers)

    @staticmethod
    def _normalize_turns(value: object) -> tuple[SpeakerTurn, ...]:
        if not isinstance(value, list):
            raise ValueError("turns must be an array")
        turns = tuple(
            SpeakerTurn.from_value(turn, index)
            for index, turn in enumerate(value)
        )
        previous_start = 0.0
        for index, turn in enumerate(turns):
            if turn.turn_id != index + 1:
                raise ValueError("turn_id values must be contiguous from 1")
            if index > 0 and turn.start_sec < previous_start:
                raise ValueError("turns must be ordered by start_sec")
            previous_start = turn.start_sec
        return turns

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
