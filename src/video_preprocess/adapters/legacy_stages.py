"""Compatibility bindings from StageTask to the current ``run(ctx)`` stages."""

from __future__ import annotations

import hashlib
import os
import tempfile
import threading
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from pipeline.context import PipelineContext
from video_preprocess.domain import (
    ArtifactRef,
    ModelExecution,
    StageResult,
    StageStatus,
    StageTask,
)
from video_preprocess.executors import StageBindingRegistry
from video_preprocess.storage import LegacyArtifactRegistrar


class LegacyStageContractError(RuntimeError):
    """A legacy path/config cannot satisfy its explicit StageTask contract."""


class LegacyStageModule(Protocol):
    """Minimal shape shared by the existing numbered Stage modules."""

    NAME: str

    def run(self, ctx: PipelineContext) -> Mapping[str, object]: ...


PathResolver = Callable[[PipelineContext, StageTask], Path]
OutputResolver = Callable[
    [PipelineContext, LegacyArtifactRegistrar, StageTask],
    Mapping[str, ArtifactRef],
]
StageHook = Callable[[PipelineContext, StageTask], None]
OutcomeResolver = Callable[
    [PipelineContext, Mapping[str, object]],
    "LegacyStageOutcome",
]


@dataclass(frozen=True, slots=True)
class LegacyStageOutcome:
    """Terminal status and effective models derived from legacy output."""

    status: StageStatus = StageStatus.SUCCEEDED
    models: Sequence[ModelExecution] = ()
    reason_code: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class LegacyInputBinding:
    """Logical task input and the host path read by a legacy Stage."""

    logical_name: str
    path: PathResolver


@dataclass(frozen=True, slots=True)
class LegacyStageDefinition:
    """Explicit compatibility contract for one current Stage module."""

    name: str
    stage_version: str
    module: LegacyStageModule
    inputs: Sequence[LegacyInputBinding]
    config_fields: Sequence[str]
    model_bindings: Mapping[str, str]
    output_resolver: OutputResolver
    before_run: StageHook | None = None
    after_run: StageHook | None = None
    outcome_resolver: OutcomeResolver | None = None


class LegacyStageTaskRunner:
    """Run one legacy Stage while enforcing explicit task semantics."""

    def __init__(
        self,
        context: PipelineContext,
        registrar: LegacyArtifactRegistrar,
        definition: LegacyStageDefinition,
        *,
        execution_lock: threading.Lock | None = None,
    ) -> None:
        if not isinstance(context, PipelineContext):
            raise TypeError("context must be a PipelineContext")
        if not callable(getattr(registrar, "register_file", None)):
            raise TypeError("registrar must implement register_file")
        if not isinstance(definition, LegacyStageDefinition):
            raise TypeError("definition must be a LegacyStageDefinition")
        self.context = context
        self.registrar = registrar
        self.definition = definition
        self.execution_lock = execution_lock or threading.Lock()

    def __call__(self, task: StageTask) -> StageResult:
        if not isinstance(task, StageTask):
            raise TypeError("task must be a StageTask")
        with self.execution_lock:
            self._validate_task(task)
            self._verify_inputs(task)
            previous_config = {
                name: getattr(self.context, name)
                for name in self.definition.config_fields
            }
            try:
                for name in self.definition.config_fields:
                    setattr(self.context, name, task.config[name])
                if self.definition.before_run is not None:
                    self.definition.before_run(self.context, task)
                metrics = self.definition.module.run(self.context)
                if not isinstance(metrics, Mapping):
                    raise LegacyStageContractError(
                        "legacy Stage must return a metrics mapping"
                    )
                if self.definition.after_run is not None:
                    self.definition.after_run(self.context, task)
                outputs = dict(
                    self.definition.output_resolver(
                        self.context,
                        self.registrar,
                        task,
                    )
                )
                outcome = (
                    LegacyStageOutcome()
                    if self.definition.outcome_resolver is None
                    else self.definition.outcome_resolver(
                        self.context,
                        metrics,
                    )
                )
                if not isinstance(outcome, LegacyStageOutcome):
                    raise LegacyStageContractError(
                        "legacy outcome resolver returned an invalid value"
                    )
            finally:
                for name, value in previous_config.items():
                    setattr(self.context, name, value)
        return StageResult(
            run_id=task.run_id,
            stage_run_id=task.stage_run_id,
            attempt=task.attempt,
            status=outcome.status,
            outputs=outputs,
            metrics=dict(metrics),
            models=outcome.models,
            reason_code=outcome.reason_code,
            reason=outcome.reason,
        )

    def _validate_task(self, task: StageTask) -> None:
        definition = self.definition
        if task.stage != definition.name:
            raise LegacyStageContractError(
                "StageTask name does not match the legacy binding"
            )
        if task.stage_version != definition.stage_version:
            raise LegacyStageContractError(
                "StageTask version does not match the legacy binding"
            )
        expected_inputs = {binding.logical_name for binding in definition.inputs}
        if set(task.inputs) != expected_inputs:
            raise LegacyStageContractError(
                "StageTask inputs do not match the legacy binding"
            )
        expected_config = set(definition.config_fields)
        if set(task.config) != expected_config:
            raise LegacyStageContractError(
                "StageTask config does not match the legacy binding"
            )
        if dict(task.model_bindings) != dict(definition.model_bindings):
            raise LegacyStageContractError(
                "StageTask model bindings do not match the legacy binding"
            )

    def _verify_inputs(self, task: StageTask) -> None:
        for binding in self.definition.inputs:
            ref = task.inputs[binding.logical_name]
            path = binding.path(self.context, task)
            _verify_path(binding.logical_name, path, ref)


def create_legacy_media_bindings(
    context: PipelineContext,
    registrar: LegacyArtifactRegistrar,
    *,
    stage_modules: Mapping[str, LegacyStageModule] | None = None,
) -> StageBindingRegistry:
    """Bind legacy Stages 01-04 for the sequential LocalExecutor."""

    modules = (
        _load_default_modules()
        if stage_modules is None
        else dict(stage_modules)
    )
    expected_names = {"01_probe", "02_scenes", "03_keyframes", "04_audio"}
    if set(modules) != expected_names:
        raise ValueError("stage_modules must define legacy Stages 01 through 04")
    _validate_modules(modules, expected_names)
    context.artifact_registrar = registrar
    return _create_binding_registry(
        context,
        registrar,
        _media_definitions(modules),
    )


def create_legacy_model_bindings(
    context: PipelineContext,
    registrar: LegacyArtifactRegistrar,
    *,
    stage_modules: Mapping[str, LegacyStageModule] | None = None,
) -> StageBindingRegistry:
    """Bind provider-backed legacy Stages 05-08."""

    modules = (
        _load_model_modules()
        if stage_modules is None
        else dict(stage_modules)
    )
    expected_names = {"05_vad", "06_stt", "07_diarize", "08_captions"}
    if set(modules) != expected_names:
        raise ValueError("stage_modules must define legacy Stages 05 through 08")
    _validate_modules(modules, expected_names)
    context.artifact_registrar = registrar
    return _create_binding_registry(
        context,
        registrar,
        _model_definitions(modules),
    )


def create_legacy_final_bindings(
    context: PipelineContext,
    registrar: LegacyArtifactRegistrar,
    *,
    stage_modules: Mapping[str, LegacyStageModule] | None = None,
) -> StageBindingRegistry:
    """Bind legacy aggregation and delivery Stages 09-11."""

    modules = (
        _load_final_modules()
        if stage_modules is None
        else dict(stage_modules)
    )
    expected_names = {"09_timeline", "10_index", "11_context"}
    if set(modules) != expected_names:
        raise ValueError("stage_modules must define legacy Stages 09 through 11")
    _validate_modules(modules, expected_names)
    context.artifact_registrar = registrar
    return _create_binding_registry(
        context,
        registrar,
        _final_definitions(modules),
    )


def create_legacy_pipeline_bindings(
    context: PipelineContext,
    registrar: LegacyArtifactRegistrar,
    *,
    stage_modules: Mapping[str, LegacyStageModule] | None = None,
) -> StageBindingRegistry:
    """Bind all legacy Stages with per-Stage context mutation guards."""

    modules = (
        {
            **_load_default_modules(),
            **_load_model_modules(),
            **_load_final_modules(),
        }
        if stage_modules is None
        else dict(stage_modules)
    )
    expected_names = {
        "01_probe",
        "02_scenes",
        "03_keyframes",
        "04_audio",
        "05_vad",
        "06_stt",
        "07_diarize",
        "08_captions",
        "09_timeline",
        "10_index",
        "11_context",
    }
    if set(modules) != expected_names:
        raise ValueError("stage_modules must define legacy Stages 01 through 11")
    _validate_modules(modules, expected_names)
    definitions = (
        *_media_definitions(modules),
        *_model_definitions(modules),
        *_final_definitions(modules),
    )
    context.artifact_registrar = registrar
    return _create_binding_registry(context, registrar, definitions)


def _create_binding_registry(
    context: PipelineContext,
    registrar: LegacyArtifactRegistrar,
    definitions: Sequence[LegacyStageDefinition],
) -> StageBindingRegistry:
    return StageBindingRegistry(
        (
            definition.name,
            LegacyStageTaskRunner(
                context,
                registrar,
                definition,
            ),
        )
        for definition in definitions
    )


def _validate_modules(
    modules: Mapping[str, LegacyStageModule],
    expected_names: set[str],
) -> None:
    for name in expected_names:
        module = modules[name]
        if getattr(module, "NAME", None) != name or not callable(
            getattr(module, "run", None)
        ):
            raise TypeError(
                f"legacy Stage module does not match binding {name}"
            )


def _media_definitions(
    modules: Mapping[str, LegacyStageModule],
) -> tuple[LegacyStageDefinition, ...]:
    video = LegacyInputBinding("video", lambda ctx, task: ctx.video_path)
    metadata = LegacyInputBinding(
        "metadata",
        lambda ctx, task: ctx.out_root / "01_probe" / "metadata.json",
    )
    scenes = LegacyInputBinding(
        "scenes",
        lambda ctx, task: ctx.out_root / "02_scenes" / "scenes.json",
    )
    return (
        LegacyStageDefinition(
            name="01_probe",
            stage_version="1.0.0",
            module=modules["01_probe"],
            inputs=(video,),
            config_fields=(),
            model_bindings={},
            output_resolver=_probe_outputs,
        ),
        LegacyStageDefinition(
            name="02_scenes",
            stage_version="1.0.0",
            module=modules["02_scenes"],
            inputs=(video, metadata),
            config_fields=("scene_threshold", "min_scene_len_frames"),
            model_bindings={},
            output_resolver=_scene_outputs,
        ),
        LegacyStageDefinition(
            name="03_keyframes",
            stage_version="1.2.0",
            module=modules["03_keyframes"],
            inputs=(video, scenes),
            config_fields=("keyframes_per_scene",),
            model_bindings={},
            output_resolver=_keyframe_outputs,
            after_run=_write_keyframe_bundle,
        ),
        LegacyStageDefinition(
            name="04_audio",
            stage_version="1.0.0",
            module=modules["04_audio"],
            inputs=(video, metadata),
            config_fields=(),
            model_bindings={},
            output_resolver=_audio_outputs,
        ),
    )


def _model_definitions(
    modules: Mapping[str, LegacyStageModule],
) -> tuple[LegacyStageDefinition, ...]:
    audio = LegacyInputBinding("audio", _legacy_audio_path)
    audio_metadata = LegacyInputBinding(
        "audio_metadata",
        lambda ctx, task: ctx.out_root / "04_audio" / "audio.json",
    )
    vad_segments = LegacyInputBinding(
        "vad_segments",
        lambda ctx, task: ctx.out_root / "05_vad" / "vad_segments.json",
    )
    keyframes = LegacyInputBinding(
        "keyframes",
        lambda ctx, task: ctx.out_root / "03_keyframes" / "keyframes.json",
    )
    keyframe_images = LegacyInputBinding(
        "keyframe_images",
        lambda ctx, task: (
            ctx.out_root / "03_keyframes" / "keyframe_images.zip"
        ),
    )
    return (
        LegacyStageDefinition(
            name="05_vad",
            stage_version="1.0.0",
            module=modules["05_vad"],
            inputs=(audio, audio_metadata),
            config_fields=("vad_min_silence_ms", "vad_speech_pad_ms"),
            model_bindings={"vad": "vad.default"},
            output_resolver=_vad_outputs,
            outcome_resolver=_vad_outcome,
        ),
        LegacyStageDefinition(
            name="06_stt",
            stage_version="1.0.0",
            module=modules["06_stt"],
            inputs=(audio, vad_segments),
            config_fields=(
                "stt_merge_gap_sec",
                "language",
                "whisper_model",
            ),
            model_bindings={"stt": "stt.default"},
            output_resolver=_stt_outputs,
            outcome_resolver=_stt_outcome,
        ),
        LegacyStageDefinition(
            name="07_diarize",
            stage_version="1.0.0",
            module=modules["07_diarize"],
            inputs=(audio,),
            config_fields=("diarize_model",),
            model_bindings={"diarization": "diarization.default"},
            output_resolver=_diarization_outputs,
            outcome_resolver=_diarization_outcome,
        ),
        LegacyStageDefinition(
            name="08_captions",
            stage_version="1.2.0",
            module=modules["08_captions"],
            inputs=(keyframes, keyframe_images),
            config_fields=("caption_model",),
            model_bindings={"caption": "caption.default"},
            output_resolver=_caption_outputs,
            before_run=_restore_keyframe_bundle,
            outcome_resolver=_caption_outcome,
        ),
    )


def _final_definitions(
    modules: Mapping[str, LegacyStageModule],
) -> tuple[LegacyStageDefinition, ...]:
    metadata = LegacyInputBinding(
        "metadata",
        lambda ctx, task: ctx.out_root / "01_probe" / "metadata.json",
    )
    scenes = LegacyInputBinding(
        "scenes",
        lambda ctx, task: ctx.out_root / "02_scenes" / "scenes.json",
    )
    keyframes = LegacyInputBinding(
        "keyframes",
        lambda ctx, task: ctx.out_root / "03_keyframes" / "keyframes.json",
    )
    transcript = LegacyInputBinding(
        "transcript",
        lambda ctx, task: ctx.out_root / "06_stt" / "transcript.json",
    )
    diarization = LegacyInputBinding(
        "diarization",
        lambda ctx, task: ctx.out_root / "07_diarize" / "diarization.json",
    )
    captions = LegacyInputBinding(
        "captions",
        lambda ctx, task: ctx.out_root / "08_captions" / "captions.json",
    )
    timeline = LegacyInputBinding(
        "timeline",
        lambda ctx, task: ctx.out_root / "09_timeline" / "timeline.json",
    )
    return (
        LegacyStageDefinition(
            name="09_timeline",
            stage_version="1.2.0",
            module=modules["09_timeline"],
            inputs=(scenes, keyframes, transcript, diarization, captions),
            config_fields=(),
            model_bindings={},
            output_resolver=_timeline_outputs,
        ),
        LegacyStageDefinition(
            name="10_index",
            stage_version="1.1.0",
            module=modules["10_index"],
            inputs=(timeline,),
            config_fields=("embed_model",),
            model_bindings={"embedding": "embedding.default"},
            output_resolver=_index_outputs,
            outcome_resolver=_index_outcome,
        ),
        LegacyStageDefinition(
            name="11_context",
            stage_version="1.1.0",
            module=modules["11_context"],
            inputs=(metadata, diarization, timeline),
            config_fields=("max_context_tokens", "context_tokenizer_model"),
            model_bindings={},
            output_resolver=_context_outputs,
        ),
    )


def _probe_outputs(
    ctx: PipelineContext,
    registrar: LegacyArtifactRegistrar,
    task: StageTask,
) -> Mapping[str, ArtifactRef]:
    return {
        "metadata": _register(
            registrar,
            task,
            "metadata",
            "01_probe/metadata.json",
            kind="json",
            media_type="application/json",
        )
    }


def _scene_outputs(
    ctx: PipelineContext,
    registrar: LegacyArtifactRegistrar,
    task: StageTask,
) -> Mapping[str, ArtifactRef]:
    return {
        "scenes": _register(
            registrar,
            task,
            "scenes",
            "02_scenes/scenes.json",
            kind="json",
            media_type="application/json",
        ),
        "scene_stats": _register(
            registrar,
            task,
            "scene_stats",
            "02_scenes/scene_stats.csv",
            kind="table",
            media_type="text/csv",
        ),
    }


def _keyframe_outputs(
    ctx: PipelineContext,
    registrar: LegacyArtifactRegistrar,
    task: StageTask,
) -> Mapping[str, ArtifactRef]:
    return {
        "keyframes": _register(
            registrar,
            task,
            "keyframes",
            "03_keyframes/keyframes.json",
            kind="json",
            media_type="application/json",
        ),
        "keyframe_images": _register(
            registrar,
            task,
            "keyframe_images",
            "03_keyframes/keyframe_images.zip",
            kind="archive",
            media_type="application/zip",
        ),
    }


def _audio_outputs(
    ctx: PipelineContext,
    registrar: LegacyArtifactRegistrar,
    task: StageTask,
) -> Mapping[str, ArtifactRef]:
    metadata = _register(
        registrar,
        task,
        "audio_metadata",
        "04_audio/audio.json",
        kind="json",
        media_type="application/json",
    )
    payload = ctx.load_json(ctx.out_root / "04_audio" / "audio.json")
    if payload.get("has_audio"):
        audio = _register(
            registrar,
            task,
            "audio",
            "04_audio/audio_16k.wav",
            kind="audio",
            media_type="audio/wav",
        )
    else:
        audio = metadata
    return {"audio": audio, "audio_metadata": metadata}


def _vad_outputs(
    ctx: PipelineContext,
    registrar: LegacyArtifactRegistrar,
    task: StageTask,
) -> Mapping[str, ArtifactRef]:
    return {
        "vad_segments": _register(
            registrar,
            task,
            "vad_segments",
            "05_vad/vad_segments.json",
            kind="json",
            media_type="application/json",
        )
    }


def _stt_outputs(
    ctx: PipelineContext,
    registrar: LegacyArtifactRegistrar,
    task: StageTask,
) -> Mapping[str, ArtifactRef]:
    return {
        "transcript": _register(
            registrar,
            task,
            "transcript",
            "06_stt/transcript.json",
            kind="json",
            media_type="application/json",
        )
    }


def _diarization_outputs(
    ctx: PipelineContext,
    registrar: LegacyArtifactRegistrar,
    task: StageTask,
) -> Mapping[str, ArtifactRef]:
    return {
        "diarization": _register(
            registrar,
            task,
            "diarization",
            "07_diarize/diarization.json",
            kind="json",
            media_type="application/json",
        )
    }


def _caption_outputs(
    ctx: PipelineContext,
    registrar: LegacyArtifactRegistrar,
    task: StageTask,
) -> Mapping[str, ArtifactRef]:
    return {
        "captions": _register(
            registrar,
            task,
            "captions",
            "08_captions/captions.json",
            kind="json",
            media_type="application/json",
        )
    }


def _timeline_outputs(
    ctx: PipelineContext,
    registrar: LegacyArtifactRegistrar,
    task: StageTask,
) -> Mapping[str, ArtifactRef]:
    return {
        "timeline": _register(
            registrar,
            task,
            "timeline",
            "09_timeline/timeline.json",
            kind="json",
            media_type="application/json",
        ),
        "timeline_markdown": _register(
            registrar,
            task,
            "timeline_markdown",
            "09_timeline/timeline.md",
            kind="document",
            media_type="text/markdown",
        ),
    }


def _index_outputs(
    ctx: PipelineContext,
    registrar: LegacyArtifactRegistrar,
    task: StageTask,
) -> Mapping[str, ArtifactRef]:
    return {
        "search_index": _register(
            registrar,
            task,
            "search_index",
            "10_index/index.db",
            kind="database",
            media_type="application/vnd.sqlite3",
        ),
        "index_summary": _register(
            registrar,
            task,
            "index_summary",
            "10_index/index_summary.json",
            kind="json",
            media_type="application/json",
        ),
    }


def _context_outputs(
    ctx: PipelineContext,
    registrar: LegacyArtifactRegistrar,
    task: StageTask,
) -> Mapping[str, ArtifactRef]:
    return {
        "context": _register(
            registrar,
            task,
            "context",
            "11_context/context.md",
            kind="document",
            media_type="text/markdown",
        ),
        "context_json": _register(
            registrar,
            task,
            "context_json",
            "11_context/context.json",
            kind="json",
            media_type="application/json",
        ),
    }


def _vad_outcome(
    ctx: PipelineContext,
    metrics: Mapping[str, object],
) -> LegacyStageOutcome:
    payload = ctx.load_json(ctx.out_root / "05_vad" / "vad_segments.json")
    if not payload.get("has_audio", True):
        return LegacyStageOutcome(
            status=StageStatus.SKIPPED,
            reason_code="NO_AUDIO",
            reason="audio input has no audio stream",
        )
    return _model_outcome(payload, "vad")


def _stt_outcome(
    ctx: PipelineContext,
    metrics: Mapping[str, object],
) -> LegacyStageOutcome:
    payload = ctx.load_json(ctx.out_root / "06_stt" / "transcript.json")
    if not payload.get("segments"):
        return LegacyStageOutcome(
            status=StageStatus.SKIPPED,
            reason_code="NO_SPEECH",
            reason="VAD produced no speech segments",
        )
    return _model_outcome(payload, "stt")


def _diarization_outcome(
    ctx: PipelineContext,
    metrics: Mapping[str, object],
) -> LegacyStageOutcome:
    payload = ctx.load_json(
        ctx.out_root / "07_diarize" / "diarization.json"
    )
    if not payload.get("available"):
        reason = payload.get("reason")
        return LegacyStageOutcome(
            status=StageStatus.SKIPPED,
            reason_code=(
                "NO_AUDIO"
                if isinstance(reason, str) and "오디오" in reason
                else "OPTIONAL_DIARIZATION_UNAVAILABLE"
            ),
            reason=(
                reason
                if isinstance(reason, str) and reason.strip()
                else "optional diarization is unavailable"
            ),
        )
    return _model_outcome(payload, "diarization")


def _caption_outcome(
    ctx: PipelineContext,
    metrics: Mapping[str, object],
) -> LegacyStageOutcome:
    payload = ctx.load_json(ctx.out_root / "08_captions" / "captions.json")
    if not payload.get("captions"):
        return LegacyStageOutcome(
            status=StageStatus.SKIPPED,
            reason_code="NO_KEYFRAMES",
            reason="keyframe input is empty",
        )
    return _model_outcome(payload, "caption")


def _index_outcome(
    ctx: PipelineContext,
    metrics: Mapping[str, object],
) -> LegacyStageOutcome:
    payload = ctx.load_json(
        ctx.out_root / "10_index" / "index_summary.json"
    )
    return _model_outcome(
        {
            "provider": payload.get("embed_provider"),
            "model": payload.get("embed_model"),
            "revision": payload.get("embed_revision"),
            "runtime": payload.get("embed_runtime"),
        },
        "embedding",
    )


def _model_outcome(
    payload: Mapping[str, object],
    slot: str,
) -> LegacyStageOutcome:
    required = ("provider", "model", "revision")
    values = {name: payload.get(name) for name in required}
    if any(
        not isinstance(value, str) or not value.strip()
        for value in values.values()
    ):
        raise LegacyStageContractError(
            f"successful {slot} output is missing effective model metadata"
        )
    runtime = payload.get("runtime")
    if runtime is not None and (
        not isinstance(runtime, str) or not runtime.strip()
    ):
        raise LegacyStageContractError(
            f"successful {slot} output has invalid runtime metadata"
        )
    return LegacyStageOutcome(
        models=(
            ModelExecution(
                slot=slot,
                provider=values["provider"],
                model=values["model"],
                revision=values["revision"],
                runtime=runtime,
            ),
        )
    )


def _register(
    registrar: LegacyArtifactRegistrar,
    task: StageTask,
    logical_name: str,
    relative_path: str,
    *,
    kind: str,
    media_type: str,
) -> ArtifactRef:
    try:
        return registrar.register_file(
            relative_path,
            artifact_id=f"{task.stage_run_id}:{logical_name}",
            kind=kind,
            media_type=media_type,
            metadata={
                "stage": task.stage,
                "logical_output": logical_name,
            },
        )
    except Exception as exc:
        raise LegacyStageContractError(
            f"legacy Stage did not publish output {logical_name}"
        ) from exc


def _write_keyframe_bundle(
    ctx: PipelineContext,
    task: StageTask,
) -> None:
    payload = ctx.load_json(
        ctx.out_root / "03_keyframes" / "keyframes.json"
    )
    keyframes = payload.get("keyframes")
    if not isinstance(keyframes, list):
        raise LegacyStageContractError(
            "keyframes.json must contain a keyframes array"
        )
    output_path = ctx.out_root / "03_keyframes" / "keyframe_images.zip"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=output_path.parent,
        prefix=".keyframe_images.",
        suffix=".tmp",
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        with zipfile.ZipFile(
            temporary_path,
            mode="w",
            compression=zipfile.ZIP_STORED,
        ) as archive:
            for entry in sorted(keyframes, key=lambda item: item["path"]):
                relative = _safe_keyframe_path(entry.get("path"))
                source = ctx.out_root.joinpath(*relative.parts)
                if not source.is_file():
                    raise LegacyStageContractError(
                        "keyframe image listed by Stage is missing"
                    )
                info = zipfile.ZipInfo(relative.as_posix())
                info.date_time = (1980, 1, 1, 0, 0, 0)
                info.compress_type = zipfile.ZIP_STORED
                info.external_attr = 0o600 << 16
                archive.writestr(info, source.read_bytes())
        os.replace(temporary_path, output_path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _restore_keyframe_bundle(
    ctx: PipelineContext,
    task: StageTask,
) -> None:
    payload = ctx.load_json(
        ctx.out_root / "03_keyframes" / "keyframes.json"
    )
    keyframes = payload.get("keyframes")
    if not isinstance(keyframes, list):
        raise LegacyStageContractError(
            "keyframes.json must contain a keyframes array"
        )
    expected = []
    for entry in keyframes:
        if not isinstance(entry, Mapping):
            raise LegacyStageContractError(
                "keyframes array must contain objects"
            )
        expected.append(_safe_keyframe_path(entry.get("path")))
    expected_names = tuple(sorted(path.as_posix() for path in expected))
    bundle = ctx.out_root / "03_keyframes" / "keyframe_images.zip"
    try:
        with zipfile.ZipFile(bundle) as archive:
            actual_names = tuple(sorted(archive.namelist()))
            if actual_names != expected_names:
                raise LegacyStageContractError(
                    "keyframe bundle members do not match keyframes.json"
                )
            for name in actual_names:
                relative = _safe_keyframe_path(name)
                target = ctx.out_root.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write_bytes(target, archive.read(name))
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise LegacyStageContractError(
            "keyframe bundle cannot be restored"
        ) from exc


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _legacy_audio_path(ctx: PipelineContext, task: StageTask) -> Path:
    metadata_path = ctx.out_root / "04_audio" / "audio.json"
    payload = ctx.load_json(metadata_path)
    if not payload.get("has_audio"):
        return metadata_path
    relative = payload.get("path")
    if not isinstance(relative, str) or not relative.strip():
        raise LegacyStageContractError(
            "audio metadata is missing its WAV path"
        )
    path = PurePosixPath(relative)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != "04_audio/audio_16k.wav"
    ):
        raise LegacyStageContractError("audio WAV path is not allowed")
    return ctx.out_root.joinpath(*path.parts)


def _safe_keyframe_path(value: object) -> PurePosixPath:
    if not isinstance(value, str) or not value.strip() or "\\" in value:
        raise LegacyStageContractError("keyframe path must be relative POSIX")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.parts[:2] != ("03_keyframes", "frames")
    ):
        raise LegacyStageContractError(
            "keyframe path must stay under 03_keyframes/frames"
        )
    return path


def _verify_path(
    logical_name: str,
    path: Path,
    ref: ArtifactRef,
) -> None:
    if ref.checksum.algorithm != "sha256":
        raise LegacyStageContractError(
            f"legacy input {logical_name} requires a SHA-256 checksum"
        )
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
    except OSError as exc:
        raise LegacyStageContractError(
            f"legacy input {logical_name} is not materialized"
        ) from exc
    if size != ref.size_bytes or digest.hexdigest() != ref.checksum.value:
        raise LegacyStageContractError(
            f"legacy input {logical_name} does not match its ArtifactRef"
        )


def _load_default_modules() -> dict[str, LegacyStageModule]:
    from pipeline.stages import (
        s01_probe,
        s02_scenes,
        s03_keyframes,
        s04_audio,
    )

    return {
        module.NAME: module
        for module in (s01_probe, s02_scenes, s03_keyframes, s04_audio)
    }


def _load_model_modules() -> dict[str, LegacyStageModule]:
    from pipeline.stages import (
        s05_vad,
        s06_stt,
        s07_diarize,
        s08_captions,
    )

    return {
        module.NAME: module
        for module in (s05_vad, s06_stt, s07_diarize, s08_captions)
    }


def _load_final_modules() -> dict[str, LegacyStageModule]:
    from pipeline.stages import s09_timeline, s10_index, s11_context

    return {
        module.NAME: module
        for module in (s09_timeline, s10_index, s11_context)
    }
