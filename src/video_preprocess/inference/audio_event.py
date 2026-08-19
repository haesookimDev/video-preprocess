"""Task-specific adapter for windowed non-speech audio events."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
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
    ProviderCapabilities,
    RequestedModel,
)

from .errors import InferenceCallError
from .gateway import InferenceGateway


AUDIO_EVENT_TAXONOMY_VERSION = "audio-events-v1"
AUDIO_EVENT_LABELS = (
    "music",
    "applause",
    "laughter",
    "alarm",
    "siren",
    "vehicle",
    "animal",
    "door",
    "impact",
    "noise",
)
AUDIO_EVENT_OVERLAP_POLICY = "merge-same-label-overlap-v1"
DEFAULT_AUDIO_EVENT_MODEL = "MIT/ast-finetuned-audioset-10-10-0.4593"
_LABEL_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class AudioWindow:
    """One absolute, half-open audio interval sent to a Provider."""

    window_id: int
    start_sec: float
    end_sec: float

    def to_dict(self) -> dict[str, object]:
        return {
            "window_id": self.window_id,
            "start_sec": self.start_sec,
            "end_sec": self.end_sec,
        }


@dataclass(frozen=True, slots=True)
class AudioEvent:
    """One normalized event produced by deterministic window aggregation."""

    event_id: int
    label: str
    confidence: float
    start_sec: float
    end_sec: float
    source_window_ids: tuple[int, ...]

    @property
    def duration_sec(self) -> float:
        return round(self.end_sec - self.start_sec, 6)

    def to_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "label": self.label,
            "confidence": self.confidence,
            "start_sec": self.start_sec,
            "end_sec": self.end_sec,
            "duration_sec": self.duration_sec,
            "source_window_ids": list(self.source_window_ids),
        }


@dataclass(frozen=True, slots=True)
class AudioEventBatch:
    """Aggregated events and the effective model that produced them."""

    events: tuple[AudioEvent, ...]
    model: EffectiveModel
    usage: dict[str, object]
    timing: dict[str, object]


@dataclass(frozen=True, slots=True)
class _Candidate:
    label: str
    confidence: float
    window: AudioWindow


class AudioEventService:
    """Window audio, chunk requests, validate labels, and merge overlap."""

    def __init__(
        self,
        gateway: InferenceGateway,
        *,
        alias: str,
        model_name: str,
        revision: str,
        timeout_sec: float = 300.0,
        batch_size: int | None = None,
    ) -> None:
        if (
            isinstance(timeout_sec, bool)
            or not isinstance(timeout_sec, (int, float))
            or not math.isfinite(float(timeout_sec))
            or timeout_sec <= 0
        ):
            raise ValueError("timeout_sec must be positive and finite")
        if batch_size is not None and (
            isinstance(batch_size, bool)
            or not isinstance(batch_size, int)
            or batch_size < 1
        ):
            raise ValueError("batch_size must be at least 1 or None")
        self.gateway = gateway
        self.alias = alias
        self.model_name = model_name
        self.revision = revision
        self.timeout_sec = float(timeout_sec)
        self.batch_size = batch_size

    def detect(
        self,
        audio: ArtifactRef,
        *,
        duration_sec: float,
        labels: Sequence[str] = AUDIO_EVENT_LABELS,
        min_confidence: float = 0.5,
        window_sec: float = 5.0,
        hop_sec: float = 2.5,
        sampling_rate: int = 16000,
        run_id: str = "compat_pipeline",
        stage_run_id: str = "audio_event_detection",
        trace_id: str | None = None,
    ) -> AudioEventBatch:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.detect_async(
                audio,
                duration_sec=duration_sec,
                labels=labels,
                min_confidence=min_confidence,
                window_sec=window_sec,
                hop_sec=hop_sec,
                sampling_rate=sampling_rate,
                run_id=run_id,
                stage_run_id=stage_run_id,
                trace_id=trace_id,
            ))
        raise RuntimeError(
            "AudioEventService.detect() cannot run inside an event loop; "
            "use await detect_async()"
        )

    async def detect_async(
        self,
        audio: ArtifactRef,
        *,
        duration_sec: float,
        labels: Sequence[str] = AUDIO_EVENT_LABELS,
        min_confidence: float = 0.5,
        window_sec: float = 5.0,
        hop_sec: float = 2.5,
        sampling_rate: int = 16000,
        run_id: str = "compat_pipeline",
        stage_run_id: str = "audio_event_detection",
        trace_id: str | None = None,
    ) -> AudioEventBatch:
        if not isinstance(audio, ArtifactRef):
            raise ValueError("audio must be an ArtifactRef")
        duration = self._positive_number(duration_sec, "duration_sec")
        window = self._positive_number(window_sec, "window_sec")
        hop = self._positive_number(hop_sec, "hop_sec")
        if hop > window:
            raise ValueError("hop_sec must not exceed window_sec")
        if (
            isinstance(sampling_rate, bool)
            or not isinstance(sampling_rate, int)
            or sampling_rate < 1
        ):
            raise ValueError("sampling_rate must be a positive integer")
        confidence = self._confidence(min_confidence)
        normalized_labels = self._labels(labels)
        windows = self._windows(duration, window, hop)
        trace_id = trace_id or f"trace_{uuid.uuid4().hex}"
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + self.timeout_sec
        capabilities = await self._capabilities(deadline)
        chunk_size = min(
            capabilities.max_batch_size,
            self.batch_size or capabilities.max_batch_size,
        )

        candidates: list[_Candidate] = []
        batch_sizes: list[int] = []
        batch_timing: list[dict[str, object]] = []
        effective_model: EffectiveModel | None = None
        for start in range(0, len(windows), chunk_size):
            window_batch = windows[start:start + chunk_size]
            request_id = f"infer_{uuid.uuid4().hex}"
            request = self._request(
                audio,
                window_batch,
                request_id=request_id,
                labels=normalized_labels,
                min_confidence=confidence,
                sampling_rate=sampling_rate,
                run_id=run_id,
                stage_run_id=stage_run_id,
                trace_id=trace_id,
                timeout_sec=self._remaining_timeout(deadline, request_id),
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
                raise self._invalid(request_id, "successful response has no model")
            if effective_model is None:
                effective_model = response.model
            elif response.model != effective_model:
                raise self._invalid(
                    request_id,
                    "audio event model metadata changed between chunks",
                )
            candidates.extend(self._results(
                response.outputs.get("results"),
                windows=window_batch,
                labels=set(normalized_labels),
                min_confidence=confidence,
                request_id=request_id,
            ))
            batch_sizes.append(len(window_batch))
            batch_timing.append(dict(response.timing))

        assert effective_model is not None
        events = self._merge(candidates)
        return AudioEventBatch(
            events=events,
            model=effective_model,
            usage={
                "audio_duration_sec": duration,
                "window_count": len(windows),
                "event_count": len(events),
                "batch_count": len(batch_sizes),
                "batch_sizes": batch_sizes,
                "configured_batch_size": self.batch_size,
                "provider_max_batch_size": capabilities.max_batch_size,
            },
            timing={
                "total_sec": round(loop.time() - started, 6),
                "inference_sec": round(sum(
                    float(item["inference_sec"])
                    for item in batch_timing
                    if isinstance(item.get("inference_sec"), (int, float))
                    and not isinstance(item["inference_sec"], bool)
                ), 6),
                "batches": batch_timing,
            },
        )

    def _request(
        self,
        audio: ArtifactRef,
        windows: list[AudioWindow],
        **values,
    ) -> InferenceRequest:
        request_id = values["request_id"]
        labels = values["labels"]
        min_confidence = values["min_confidence"]
        sampling_rate = values["sampling_rate"]
        window_dicts = [window.to_dict() for window in windows]
        semantics = {
            "alias": self.alias,
            "model": self.model_name,
            "revision": self.revision,
            "audio": audio.to_dict(),
            "windows": window_dicts,
            "labels": list(labels),
            "min_confidence": min_confidence,
            "sampling_rate": sampling_rate,
        }
        fingerprint = hashlib.sha256(json.dumps(
            semantics,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        return InferenceRequest(
            request_id=request_id,
            idempotency_key=f"audio_event_{fingerprint}",
            run_id=values["run_id"],
            stage_run_id=values["stage_run_id"],
            task=InferenceTask.AUDIO_EVENT_DETECTION,
            model=RequestedModel(
                alias=self.alias,
                name=self.model_name,
                revision=self.revision,
            ),
            inputs={"audio": audio, "windows": window_dicts},
            parameters={
                "taxonomy_version": AUDIO_EVENT_TAXONOMY_VERSION,
                "labels": list(labels),
                "min_confidence": min_confidence,
                "sampling_rate": sampling_rate,
                "interval": "half-open",
            },
            timeout_sec=values["timeout_sec"],
            trace_id=values["trace_id"],
        )

    async def _capabilities(self, deadline: float) -> ProviderCapabilities:
        request_id = f"infer_{uuid.uuid4().hex}"
        try:
            capabilities = await asyncio.wait_for(
                self.gateway.capabilities(self.alias),
                timeout=self._remaining_timeout(deadline, request_id),
            )
        except asyncio.TimeoutError as exc:
            raise self._call_error(
                request_id,
                InferenceErrorCode.PROVIDER_TIMEOUT,
                "provider capability check timed out",
                retryable=True,
            ) from exc
        except InferenceCallError:
            raise
        except Exception as exc:
            raise self._call_error(
                request_id,
                InferenceErrorCode.PROVIDER_UNAVAILABLE,
                "provider capability check failed",
                details={"error_type": type(exc).__name__},
            ) from exc
        if not isinstance(capabilities, ProviderCapabilities):
            raise self._call_error(
                request_id,
                InferenceErrorCode.PROVIDER_UNAVAILABLE,
                "provider returned invalid capabilities",
            )
        if (
            self.alias not in capabilities.model_aliases
            or InferenceTask.AUDIO_EVENT_DETECTION not in capabilities.tasks
        ):
            raise self._call_error(
                request_id,
                InferenceErrorCode.UNSUPPORTED_CAPABILITY,
                "provider does not support the requested audio event model",
            )
        return capabilities

    @staticmethod
    def _windows(duration: float, window: float, hop: float) -> list[AudioWindow]:
        values = []
        start = 0.0
        while start < duration:
            values.append(AudioWindow(
                window_id=len(values) + 1,
                start_sec=round(start, 6),
                end_sec=round(min(start + window, duration), 6),
            ))
            start += hop
        return values

    @classmethod
    def _results(
        cls,
        value: object,
        *,
        windows: list[AudioWindow],
        labels: set[str],
        min_confidence: float,
        request_id: str,
    ) -> list[_Candidate]:
        if not isinstance(value, list) or len(value) != len(windows):
            raise cls._invalid(
                request_id,
                "audio event response count does not match input windows",
            )
        candidates = []
        for index, (result, window) in enumerate(zip(value, windows)):
            if not isinstance(result, Mapping):
                raise cls._invalid(request_id, f"result {index} must be an object")
            if result.get("window_id") != window.window_id:
                raise cls._invalid(request_id, f"result {index} is out of order")
            result_labels = result.get("labels")
            if not isinstance(result_labels, list):
                raise cls._invalid(request_id, f"result {index} labels must be an array")
            seen = set()
            for label_index, item in enumerate(result_labels):
                if not isinstance(item, Mapping):
                    raise cls._invalid(request_id, "label result must be an object")
                label = item.get("label")
                if label not in labels or label in seen:
                    raise cls._invalid(
                        request_id,
                        f"result {index} label {label_index} is invalid or duplicated",
                    )
                seen.add(label)
                confidence = cls._response_confidence(
                    item.get("confidence"), request_id
                )
                if confidence >= min_confidence:
                    candidates.append(_Candidate(label, confidence, window))
        return candidates

    @staticmethod
    def _merge(candidates: list[_Candidate]) -> tuple[AudioEvent, ...]:
        groups: list[dict[str, object]] = []
        for candidate in sorted(
            candidates,
            key=lambda item: (
                item.label,
                item.window.start_sec,
                item.window.end_sec,
            ),
        ):
            current = groups[-1] if groups else None
            if (
                current is None
                or current["label"] != candidate.label
                or candidate.window.start_sec > current["end_sec"]
            ):
                groups.append({
                    "label": candidate.label,
                    "confidence": candidate.confidence,
                    "start_sec": candidate.window.start_sec,
                    "end_sec": candidate.window.end_sec,
                    "source_window_ids": [candidate.window.window_id],
                })
                continue
            current["confidence"] = max(
                current["confidence"], candidate.confidence
            )
            current["end_sec"] = max(
                current["end_sec"], candidate.window.end_sec
            )
            current["source_window_ids"].append(candidate.window.window_id)
        ordered = sorted(
            groups,
            key=lambda item: (item["start_sec"], item["end_sec"], item["label"]),
        )
        return tuple(
            AudioEvent(
                event_id=index,
                label=item["label"],
                confidence=round(item["confidence"], 6),
                start_sec=item["start_sec"],
                end_sec=item["end_sec"],
                source_window_ids=tuple(item["source_window_ids"]),
            )
            for index, item in enumerate(ordered, start=1)
        )

    @staticmethod
    def _labels(labels: Sequence[str]) -> tuple[str, ...]:
        if isinstance(labels, (str, bytes)) or not isinstance(labels, Sequence):
            raise ValueError("labels must be a sequence")
        normalized = []
        for index, label in enumerate(labels):
            if not isinstance(label, str) or not _LABEL_PATTERN.fullmatch(label):
                raise ValueError(f"labels[{index}] is not a canonical label")
            if label not in AUDIO_EVENT_LABELS:
                raise ValueError(f"labels[{index}] is outside taxonomy v1")
            if label not in normalized:
                normalized.append(label)
        if not normalized:
            raise ValueError("labels must not be empty")
        return tuple(normalized)

    @staticmethod
    def _positive_number(value: object, name: str) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value <= 0
        ):
            raise ValueError(f"{name} must be positive and finite")
        return float(value)

    @staticmethod
    def _confidence(value: object) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0 <= value <= 1
        ):
            raise ValueError("min_confidence must be between 0 and 1")
        return float(value)

    @classmethod
    def _response_confidence(cls, value: object, request_id: str) -> float:
        try:
            return cls._confidence(value)
        except ValueError as exc:
            raise cls._invalid(request_id, "result confidence must be 0..1") from exc

    @staticmethod
    def _remaining_timeout(deadline: float, request_id: str) -> float:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise AudioEventService._call_error(
                request_id,
                InferenceErrorCode.PROVIDER_TIMEOUT,
                "audio event inference deadline elapsed between batches",
                retryable=True,
            )
        return remaining

    @staticmethod
    def _invalid(request_id: str, message: str) -> InferenceCallError:
        return AudioEventService._call_error(
            request_id,
            InferenceErrorCode.INFERENCE_FAILED,
            message,
        )

    @staticmethod
    def _call_error(
        request_id: str,
        code: InferenceErrorCode,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, object] | None = None,
    ) -> InferenceCallError:
        return InferenceCallError(InferenceFailure(
            code=code,
            message=message,
            retryable=retryable,
            details={} if details is None else details,
            request_id=request_id,
        ))
