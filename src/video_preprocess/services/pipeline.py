"""Application use case for planning and running one preprocessing request."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from video_preprocess.domain import ArtifactRef
from video_preprocess.engine import DAGPlanner, PipelineRunResult
from video_preprocess.engine.planner import ExecutionPlan


class PipelineServiceInputError(ValueError):
    """A run request cannot be satisfied at the application boundary."""


@dataclass(frozen=True, slots=True)
class PipelineSettings:
    """User-controlled settings mapped to exact legacy Stage contracts."""

    scene_threshold: float = 27.0
    min_scene_len_frames: int = 15
    keyframes_per_scene: int = 1
    vad_min_silence_ms: int = 500
    vad_speech_pad_ms: int = 200
    stt_merge_gap_sec: float = 0.5
    whisper_model: str = "base"
    language: str | None = None
    caption_model: str = "Salesforce/blip-image-captioning-base"
    embed_model: str = "paraphrase-multilingual-MiniLM-L12-v2"
    diarize_model: str = "pyannote/speaker-diarization-community-1"

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
            "06_stt": {
                "stt_merge_gap_sec": self.stt_merge_gap_sec,
                "language": self.language,
                "whisper_model": self.whisper_model,
            },
            "07_diarize": {"diarize_model": self.diarize_model},
            "08_captions": {"caption_model": self.caption_model},
            "10_index": {"embed_model": self.embed_model},
        }

    @staticmethod
    def model_bindings() -> dict[str, dict[str, str]]:
        return {
            "05_vad": {"vad": "vad.default"},
            "06_stt": {"stt": "stt.default"},
            "07_diarize": {"diarization": "diarization.default"},
            "08_captions": {"caption": "caption.default"},
            "10_index": {"embedding": "embedding.default"},
        }


@dataclass(frozen=True, slots=True)
class PipelineRunRequest:
    """Adapter-neutral request for a full or selected pipeline run."""

    video_path: Path
    output_root: Path
    settings: PipelineSettings = field(default_factory=PipelineSettings)
    run_id: str | None = None
    trace_id: str | None = None
    stage: str | None = None
    from_stage: str | None = None
    to_stage: str | None = None
    force_stages: Collection[str] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "video_path", Path(self.video_path))
        object.__setattr__(self, "output_root", Path(self.output_root))
        if not isinstance(self.settings, PipelineSettings):
            raise TypeError("settings must be PipelineSettings")
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
    ) -> PipelineRunResult: ...


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
        if not callable(getattr(runtime_factory, "create", None)):
            raise TypeError("runtime_factory must implement create")
        self.planner = planner
        self.runtime_factory = runtime_factory
        self.run_id_factory = run_id_factory or _new_run_id
        self.trace_id_factory = trace_id_factory or _new_trace_id

    async def run(self, request: PipelineRunRequest) -> PipelineRunResult:
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
        return await runtime.engine.run(
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
