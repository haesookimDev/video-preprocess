"""Local filesystem and in-process model composition for the application."""

from __future__ import annotations

import hashlib
import mimetypes
from collections.abc import Callable, Collection, Mapping
from pathlib import Path

from pipeline.context import PipelineContext
from pipeline.preflight import load_hf_token
from video_preprocess.adapters import create_legacy_pipeline_bindings
from video_preprocess.engine import ManifestCacheEvaluator, PipelineEngine
from video_preprocess.executors import LocalExecutor
from video_preprocess.inference.local import (
    create_local_caption_service,
    create_local_diarization_service,
    create_local_stt_service,
    create_local_vad_service,
)
from video_preprocess.storage import (
    LocalArtifactStore,
    LocalRunStore,
    LegacyOutputAdapter,
)

from .pipeline import (
    PipelineRunRequest,
    PipelineRuntime,
    PipelineServiceInputError,
)


ContextConfigurer = Callable[
    [PipelineContext, LocalArtifactStore],
    None,
]


class LocalPipelineRuntimeFactory:
    """Compose one local Engine runtime and restore partial-run inputs."""

    def __init__(
        self,
        *,
        stage_modules: Mapping[str, object] | None = None,
        context_configurer: ContextConfigurer | None = None,
        project_root: Path | None = None,
    ) -> None:
        self.stage_modules = (
            None if stage_modules is None else dict(stage_modules)
        )
        self.project_root = (
            Path(__file__).resolve().parents[3]
            if project_root is None
            else Path(project_root).resolve()
        )
        self.context_configurer = (
            self._configure_local_inference
            if context_configurer is None
            else context_configurer
        )
        if not callable(self.context_configurer):
            raise TypeError("context_configurer must be callable")

    def create(
        self,
        request: PipelineRunRequest,
        *,
        run_id: str,
        boundary_inputs: Collection[str],
    ) -> PipelineRuntime:
        output_root = request.output_root.resolve()
        output_root.mkdir(parents=True, exist_ok=True)
        namespace = "local-" + hashlib.sha256(
            str(output_root).encode("utf-8")
        ).hexdigest()[:16]
        artifact_store = LocalArtifactStore(
            output_root,
            namespace=namespace,
        )
        run_store = LocalRunStore(output_root, artifact_store)
        video = self._ingest_video(request.video_path, artifact_store)
        artifacts = self._restore_boundary_artifacts(
            run_store,
            artifact_store,
            run_id=run_id,
            boundary_inputs=boundary_inputs,
            video=video,
        )
        settings = request.settings
        context = PipelineContext(
            video_path=request.video_path.resolve(),
            out_root=output_root,
            scene_threshold=settings.scene_threshold,
            min_scene_len_frames=settings.min_scene_len_frames,
            keyframes_per_scene=settings.keyframes_per_scene,
            vad_min_silence_ms=settings.vad_min_silence_ms,
            vad_speech_pad_ms=settings.vad_speech_pad_ms,
            stt_merge_gap_sec=settings.stt_merge_gap_sec,
            whisper_model=settings.whisper_model,
            language=settings.language,
            caption_model=settings.caption_model,
            embed_model=settings.embed_model,
            diarize_model=settings.diarize_model,
        )
        context.artifact_registrar = LegacyOutputAdapter(artifact_store)
        self.context_configurer(context, artifact_store)
        bindings = create_legacy_pipeline_bindings(
            context,
            context.artifact_registrar,
            stage_modules=self.stage_modules,
        )
        engine = PipelineEngine(
            LocalExecutor(bindings),
            run_store=run_store,
            cache_evaluator=ManifestCacheEvaluator(artifact_store),
        )
        return PipelineRuntime(engine=engine, artifacts=artifacts)

    def _configure_local_inference(
        self,
        context: PipelineContext,
        artifact_store: LocalArtifactStore,
    ) -> None:
        context.caption_service = create_local_caption_service(
            context.caption_model,
            artifact_store,
        )
        context.stt_service = create_local_stt_service(
            context.whisper_model,
            artifact_store,
        )
        context.diarization_service = create_local_diarization_service(
            context.diarize_model,
            artifact_store,
            token=load_hf_token(self.project_root),
        )
        context.vad_service = create_local_vad_service(artifact_store)

    @staticmethod
    def _ingest_video(
        video_path: Path,
        artifact_store: LocalArtifactStore,
    ):
        resolved = video_path.resolve()
        media_type = mimetypes.guess_type(resolved.name)[0]
        if media_type is None:
            media_type = "application/octet-stream"
        suffix = resolved.suffix.lower() or ".bin"
        with resolved.open("rb") as handle:
            pending = artifact_store.put(
                handle,
                artifact_id="input-video",
                relative_path=f"00_input/video{suffix}",
                kind="video",
                media_type=media_type,
                metadata={"source_name": resolved.name},
            )
        return artifact_store.publish(pending)

    @staticmethod
    def _restore_boundary_artifacts(
        run_store: LocalRunStore,
        artifact_store: LocalArtifactStore,
        *,
        run_id: str,
        boundary_inputs: Collection[str],
        video,
    ) -> dict:
        required = set(boundary_inputs)
        artifacts = {"video": video}
        previous_required = required - {"video"}
        if not previous_required:
            return artifacts
        previous_run = run_store.load_run(run_id)
        if previous_run is None:
            raise PipelineServiceInputError(
                "partial execution requires a previous run manifest"
            )
        previous_video = previous_run.input_artifacts.get("video")
        if (
            previous_video is None
            or previous_video.size_bytes != video.size_bytes
            or previous_video.checksum != video.checksum
        ):
            raise PipelineServiceInputError(
                "partial execution video does not match the previous run"
            )
        candidates = dict(previous_run.input_artifacts)
        for stage_reference in previous_run.stages:
            manifest = run_store.load_stage(run_id, stage_reference)
            if manifest is not None:
                candidates.update(manifest.result.outputs)
        for name in sorted(previous_required):
            ref = candidates.get(name)
            if ref is None:
                continue
            verification = artifact_store.verify(ref)
            if verification.ok:
                artifacts[name] = ref
        return artifacts
