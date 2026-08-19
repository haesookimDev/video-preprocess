"""Application use case for planning and running one preprocessing request."""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from video_preprocess.domain import ArtifactRef
from video_preprocess.engine import (
    DAGPlanner,
    PipelinePreviewResult,
    PipelineRunResult,
    RetryPolicy,
)
from video_preprocess.executors import CancellationToken
from video_preprocess.inference import (
    AUDIO_EVENT_LABELS,
    InferenceDeploymentSettings,
)
from video_preprocess.engine.planner import ExecutionPlan
from video_preprocess.tokenization import sentence_transformer_tokenizer_model


class PipelineServiceInputError(ValueError):
    """A run request cannot be satisfied at the application boundary."""


_OCR_LANGUAGE_PATTERN = re.compile(r"^[a-z0-9_]+$")


@dataclass(frozen=True, slots=True)
class PipelineSettings:
    """User-controlled settings mapped to exact legacy Stage contracts."""

    scene_threshold: float = 27.0
    min_scene_len_frames: int = 15
    keyframes_per_scene: int = 1
    vad_min_silence_ms: int = 500
    vad_speech_pad_ms: int = 200
    audio_event_mode: str = "disabled"
    audio_event_model: str = "audio-event-classifier"
    audio_event_labels: tuple[str, ...] = AUDIO_EVENT_LABELS
    audio_event_min_confidence: float = 0.5
    audio_event_window_sec: float = 5.0
    audio_event_hop_sec: float = 2.5
    stt_merge_gap_sec: float = 0.5
    whisper_model: str = "base"
    language: str | None = None
    caption_model: str = "Salesforce/blip-image-captioning-base"
    ocr_mode: str = "disabled"
    ocr_model: str = "tesseract"
    ocr_languages: tuple[str, ...] = ("eng",)
    ocr_detect_orientation: bool = True
    ocr_min_confidence: float = 0.5
    embed_model: str = "paraphrase-multilingual-MiniLM-L12-v2"
    diarize_model: str = "pyannote/speaker-diarization-community-1"
    max_context_tokens: int | None = None
    context_tokenizer_model: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.audio_event_mode, str)
            or self.audio_event_mode not in {"disabled", "all"}
        ):
            raise ValueError("audio_event_mode must be disabled or all")
        if (
            not isinstance(self.audio_event_model, str)
            or not self.audio_event_model.strip()
        ):
            raise ValueError("audio_event_model must be a non-empty string")
        normalized_audio_labels = []
        if (
            isinstance(self.audio_event_labels, (str, bytes))
            or not isinstance(self.audio_event_labels, Sequence)
        ):
            raise ValueError("audio_event_labels must be a sequence")
        for label in self.audio_event_labels:
            if label not in AUDIO_EVENT_LABELS:
                raise ValueError("audio_event_labels must use taxonomy v1")
            if label not in normalized_audio_labels:
                normalized_audio_labels.append(label)
        if not normalized_audio_labels:
            raise ValueError("audio_event_labels must not be empty")
        object.__setattr__(
            self,
            "audio_event_labels",
            tuple(normalized_audio_labels),
        )
        for field_name in (
            "audio_event_min_confidence",
            "audio_event_window_sec",
            "audio_event_hop_sec",
        ):
            value = getattr(self, field_name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ValueError(f"{field_name} must be a finite number")
            object.__setattr__(self, field_name, float(value))
        if not 0 <= self.audio_event_min_confidence <= 1:
            raise ValueError(
                "audio_event_min_confidence must be between 0 and 1"
            )
        if self.audio_event_window_sec <= 0:
            raise ValueError("audio_event_window_sec must be positive")
        if not 0 < self.audio_event_hop_sec <= self.audio_event_window_sec:
            raise ValueError(
                "audio_event_hop_sec must be positive and not exceed window"
            )
        if (
            not isinstance(self.ocr_mode, str)
            or self.ocr_mode not in {"disabled", "all", "caption-hints"}
        ):
            raise ValueError(
                "ocr_mode must be disabled, all, or caption-hints"
            )
        if not isinstance(self.ocr_model, str) or not self.ocr_model.strip():
            raise ValueError("ocr_model must be a non-empty string")
        if isinstance(self.ocr_languages, (str, bytes)) or not isinstance(
            self.ocr_languages,
            Sequence,
        ):
            raise ValueError("ocr_languages must be a sequence")
        normalized_languages = []
        for language in self.ocr_languages:
            if not isinstance(language, str) or not language.strip():
                raise ValueError(
                    "ocr_languages must contain non-empty strings"
                )
            normalized = language.strip().lower()
            if not _OCR_LANGUAGE_PATTERN.fullmatch(normalized):
                raise ValueError(
                    "ocr_languages must use lowercase letters, digits, "
                    "or underscore"
                )
            if normalized not in normalized_languages:
                normalized_languages.append(normalized)
        if not normalized_languages:
            raise ValueError("ocr_languages must not be empty")
        object.__setattr__(self, "ocr_languages", tuple(normalized_languages))
        if not isinstance(self.ocr_detect_orientation, bool):
            raise ValueError("ocr_detect_orientation must be a boolean")
        if (
            isinstance(self.ocr_min_confidence, bool)
            or not isinstance(self.ocr_min_confidence, (int, float))
            or not math.isfinite(float(self.ocr_min_confidence))
            or not 0 <= float(self.ocr_min_confidence) <= 1
        ):
            raise ValueError("ocr_min_confidence must be between 0 and 1")
        object.__setattr__(
            self,
            "ocr_min_confidence",
            float(self.ocr_min_confidence),
        )
        if (
            isinstance(self.keyframes_per_scene, bool)
            or not isinstance(self.keyframes_per_scene, int)
            or not 1 <= self.keyframes_per_scene <= 3
        ):
            raise ValueError("keyframes_per_scene must be between 1 and 3")
        if self.max_context_tokens is not None and (
            isinstance(self.max_context_tokens, bool)
            or not isinstance(self.max_context_tokens, int)
            or self.max_context_tokens < 128
        ):
            raise ValueError("max_context_tokens must be at least 128 or None")
        if self.context_tokenizer_model is not None and (
            not isinstance(self.context_tokenizer_model, str)
            or not self.context_tokenizer_model.strip()
        ):
            raise ValueError(
                "context_tokenizer_model must be non-empty or None"
            )

    def stage_configs(self) -> dict[str, dict[str, object]]:
        return {
            "02_scenes": {
                "scene_threshold": self.scene_threshold,
                "min_scene_len_frames": self.min_scene_len_frames,
            },
            "03_keyframes": {
                "keyframes_per_scene": self.keyframes_per_scene,
            },
            "05_vad": {
                "vad_min_silence_ms": self.vad_min_silence_ms,
                "vad_speech_pad_ms": self.vad_speech_pad_ms,
            },
            "05_audio_events": {
                "audio_event_mode": self.audio_event_mode,
                "audio_event_model": self.audio_event_model,
                "audio_event_labels": list(self.audio_event_labels),
                "audio_event_min_confidence": (
                    self.audio_event_min_confidence
                ),
                "audio_event_window_sec": self.audio_event_window_sec,
                "audio_event_hop_sec": self.audio_event_hop_sec,
            },
            "06_stt": {
                "stt_merge_gap_sec": self.stt_merge_gap_sec,
                "language": self.language,
                "whisper_model": self.whisper_model,
            },
            "07_diarize": {"diarize_model": self.diarize_model},
            "08_captions": {"caption_model": self.caption_model},
            "08_ocr": {
                "ocr_mode": self.ocr_mode,
                "ocr_model": self.ocr_model,
                "ocr_languages": list(self.ocr_languages),
                "ocr_detect_orientation": self.ocr_detect_orientation,
                "ocr_min_confidence": self.ocr_min_confidence,
            },
            "10_index": {"embed_model": self.embed_model},
            "11_context": {
                "max_context_tokens": self.max_context_tokens,
                "context_tokenizer_model": (
                    self.context_tokenizer_model
                    or sentence_transformer_tokenizer_model(self.embed_model)
                ),
            },
        }

    @staticmethod
    def model_bindings() -> dict[str, dict[str, str]]:
        return {
            "05_audio_events": {
                "audio_event": "audio_event.default",
            },
            "05_vad": {"vad": "vad.default"},
            "06_stt": {"stt": "stt.default"},
            "07_diarize": {"diarization": "diarization.default"},
            "08_captions": {"caption": "caption.default"},
            "08_ocr": {"ocr": "ocr.default"},
            "10_index": {"embedding": "embedding.default"},
        }


@dataclass(frozen=True, slots=True)
class PipelineRunRequest:
    """Adapter-neutral request for a full or selected pipeline run."""

    video_path: Path
    output_root: Path
    settings: PipelineSettings = field(default_factory=PipelineSettings)
    deployments: InferenceDeploymentSettings = field(
        default_factory=InferenceDeploymentSettings
    )
    run_id: str | None = None
    trace_id: str | None = None
    stage: str | None = None
    from_stage: str | None = None
    to_stage: str | None = None
    force_stages: Collection[str] = ()
    stage_timeout_sec: float | None = None
    max_stage_attempts: int = 1
    retry_backoff_sec: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "video_path", Path(self.video_path))
        object.__setattr__(self, "output_root", Path(self.output_root))
        if not isinstance(self.settings, PipelineSettings):
            raise TypeError("settings must be PipelineSettings")
        if not isinstance(self.deployments, InferenceDeploymentSettings):
            raise TypeError(
                "deployments must be InferenceDeploymentSettings"
            )
        for field_name in (
            "run_id",
            "trace_id",
            "stage",
            "from_stage",
            "to_stage",
        ):
            value = getattr(self, field_name)
            if value is not None and (
                not isinstance(value, str) or not value.strip()
            ):
                raise ValueError(f"{field_name} must be non-empty or None")
        if isinstance(self.force_stages, (str, bytes)) or not isinstance(
            self.force_stages,
            Collection,
        ):
            raise TypeError("force_stages must be a collection")
        normalized_force = []
        for stage_name in self.force_stages:
            if not isinstance(stage_name, str) or not stage_name.strip():
                raise ValueError(
                    "force_stages must contain non-empty Stage names"
                )
            normalized_force.append(stage_name.strip())
        object.__setattr__(self, "force_stages", tuple(normalized_force))
        if self.stage_timeout_sec is not None and (
            isinstance(self.stage_timeout_sec, bool)
            or not isinstance(self.stage_timeout_sec, (int, float))
            or self.stage_timeout_sec <= 0
        ):
            raise ValueError("stage_timeout_sec must be positive or None")
        if self.stage_timeout_sec is not None:
            object.__setattr__(
                self,
                "stage_timeout_sec",
                float(self.stage_timeout_sec),
            )
        if (
            isinstance(self.max_stage_attempts, bool)
            or not isinstance(self.max_stage_attempts, int)
            or self.max_stage_attempts < 1
        ):
            raise ValueError("max_stage_attempts must be a positive integer")
        if (
            isinstance(self.retry_backoff_sec, bool)
            or not isinstance(self.retry_backoff_sec, (int, float))
            or self.retry_backoff_sec < 0
        ):
            raise ValueError("retry_backoff_sec must be non-negative")
        object.__setattr__(
            self,
            "retry_backoff_sec",
            float(self.retry_backoff_sec),
        )


class PipelineExecutionEngine(Protocol):
    async def run(
        self,
        plan,
        *,
        run_id: str,
        trace_id: str,
        artifacts: Mapping[str, ArtifactRef],
        stage_configs: Mapping[str, Mapping[str, object]],
        model_bindings: Mapping[str, Mapping[str, str]],
        force_stages: Collection[str],
        stage_timeouts: Mapping[str, float],
        retry_policy: RetryPolicy,
        cancellation: CancellationToken | None,
    ) -> PipelineRunResult: ...

    async def preview(
        self,
        plan,
        *,
        run_id: str,
        trace_id: str,
        artifacts: Mapping[str, ArtifactRef],
        stage_configs: Mapping[str, Mapping[str, object]],
        model_bindings: Mapping[str, Mapping[str, str]],
        force_stages: Collection[str],
    ) -> PipelinePreviewResult: ...


@dataclass(frozen=True, slots=True)
class PipelineRuntime:
    """Per-request Engine and boundary artifacts built by a composition root."""

    engine: PipelineExecutionEngine
    artifacts: Mapping[str, ArtifactRef]

    def __post_init__(self) -> None:
        if not callable(getattr(self.engine, "run", None)):
            raise TypeError("engine must implement run")
        normalized = dict(self.artifacts)
        if not all(
            isinstance(name, str) and isinstance(ref, ArtifactRef)
            for name, ref in normalized.items()
        ):
            raise TypeError("artifacts must map names to ArtifactRef values")
        object.__setattr__(self, "artifacts", normalized)


class PipelineRuntimeFactory(Protocol):
    def create(
        self,
        request: PipelineRunRequest,
        *,
        run_id: str,
        boundary_inputs: Collection[str],
    ) -> PipelineRuntime: ...

    def create_preview(
        self,
        request: PipelineRunRequest,
        *,
        run_id: str,
        boundary_inputs: Collection[str],
    ) -> PipelineRuntime: ...


IdentifierFactory = Callable[[], str]


class PipelineApplicationService:
    """Validate, plan, compose and execute one pipeline use case."""

    def __init__(
        self,
        planner: DAGPlanner,
        runtime_factory: PipelineRuntimeFactory,
        *,
        run_id_factory: IdentifierFactory | None = None,
        trace_id_factory: IdentifierFactory | None = None,
    ) -> None:
        if not isinstance(planner, DAGPlanner):
            raise TypeError("planner must be a DAGPlanner")
        for method_name in ("create", "create_preview"):
            if not callable(getattr(runtime_factory, method_name, None)):
                raise TypeError(
                    f"runtime_factory must implement {method_name}"
                )
        self.planner = planner
        self.runtime_factory = runtime_factory
        self.run_id_factory = run_id_factory or _new_run_id
        self.trace_id_factory = trace_id_factory or _new_trace_id

    async def run(
        self,
        request: PipelineRunRequest,
        *,
        cancellation: CancellationToken | None = None,
    ) -> PipelineRunResult:
        plan = self.plan(request)
        run_id = request.run_id or self._new_identifier(
            self.run_id_factory,
            "run_id",
        )
        trace_id = request.trace_id or self._new_identifier(
            self.trace_id_factory,
            "trace_id",
        )
        runtime = self.runtime_factory.create(
            request,
            run_id=run_id,
            boundary_inputs=plan.boundary_inputs,
        )
        missing = sorted(set(plan.boundary_inputs) - set(runtime.artifacts))
        if missing:
            raise PipelineServiceInputError(
                "runtime could not resolve boundary input: "
                + ", ".join(missing)
            )
        stage_configs, model_bindings = self._stage_options(request, plan)
        stage_timeouts = (
            {}
            if request.stage_timeout_sec is None
            else {
                stage_name: request.stage_timeout_sec
                for stage_name in plan.stage_names
            }
        )
        return await runtime.engine.run(
            plan,
            run_id=run_id,
            trace_id=trace_id,
            artifacts=runtime.artifacts,
            stage_configs=stage_configs,
            model_bindings=model_bindings,
            force_stages=request.force_stages,
            stage_timeouts=stage_timeouts,
            retry_policy=RetryPolicy(
                max_attempts=request.max_stage_attempts,
                initial_backoff_sec=request.retry_backoff_sec,
            ),
            cancellation=cancellation,
        )

    async def preview(
        self,
        request: PipelineRunRequest,
    ) -> PipelinePreviewResult:
        """Return the read-only cache disposition for one request."""

        plan = self.plan(request)
        run_id = request.run_id or self._new_identifier(
            self.run_id_factory,
            "run_id",
        )
        trace_id = request.trace_id or self._new_identifier(
            self.trace_id_factory,
            "trace_id",
        )
        runtime = self.runtime_factory.create_preview(
            request,
            run_id=run_id,
            boundary_inputs=plan.boundary_inputs,
        )
        preview = getattr(runtime.engine, "preview", None)
        if not callable(preview):
            raise TypeError("preview runtime engine must implement preview")
        stage_configs, model_bindings = self._stage_options(request, plan)
        return await preview(
            plan,
            run_id=run_id,
            trace_id=trace_id,
            artifacts=runtime.artifacts,
            stage_configs=stage_configs,
            model_bindings=model_bindings,
            force_stages=request.force_stages,
        )

    def plan(self, request: PipelineRunRequest) -> ExecutionPlan:
        """Validate a request and return its execution-free DAG view."""

        if not isinstance(request, PipelineRunRequest):
            raise TypeError("request must be a PipelineRunRequest")
        if not request.video_path.is_file():
            raise PipelineServiceInputError(
                f"video file does not exist: {request.video_path}"
            )
        return self.planner.plan(
            stage=request.stage,
            from_stage=request.from_stage,
            to_stage=request.to_stage,
        )

    @staticmethod
    def _stage_options(
        request: PipelineRunRequest,
        plan: ExecutionPlan,
    ) -> tuple[dict[str, dict[str, object]], dict[str, dict[str, str]]]:
        planned = set(plan.stage_names)
        stage_configs = {
            name: values
            for name, values in request.settings.stage_configs().items()
            if name in planned
        }
        model_bindings = {
            name: values
            for name, values in request.settings.model_bindings().items()
            if name in planned
        }
        return stage_configs, model_bindings

    @staticmethod
    def _new_identifier(
        factory: IdentifierFactory,
        field_name: str,
    ) -> str:
        value = factory()
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError(f"{field_name} factory returned an invalid ID")
        return value.strip()


def _new_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"run_{timestamp}_{uuid.uuid4().hex[:8]}"


def _new_trace_id() -> str:
    return f"trace_{uuid.uuid4().hex}"
