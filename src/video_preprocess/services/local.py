"""Local filesystem and in-process model composition for the application."""

from __future__ import annotations

import hashlib
import mimetypes
from collections.abc import Callable, Collection, Mapping
from pathlib import Path

from pipeline.context import PipelineContext
from pipeline.logging_setup import setup_logging
from pipeline.preflight import load_hf_token
from video_preprocess.adapters import create_legacy_pipeline_bindings
from video_preprocess.domain import ArtifactRef, Checksum
from video_preprocess.engine import ManifestCacheEvaluator, PipelineEngine
from video_preprocess.executors import LocalExecutor
from video_preprocess.inference import (
    GatewayEffectiveModelResolver,
    InferenceDeploymentSettings,
    create_configured_embedding_service,
)
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
from video_preprocess.tokenization import (
    HuggingFaceTokenCounter,
    sentence_transformer_tokenizer_model,
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


class _PreviewExecutor:
    """Executor guard used by runtimes that must remain read-only."""

    async def submit(self, task, *, control=None):
        raise RuntimeError("preview runtime cannot submit Stage tasks")

    async def status(self, handle):
        raise RuntimeError("preview runtime has no execution status")

    async def result(self, handle):
        raise RuntimeError("preview runtime has no execution results")

    async def cancel(self, handle):
        raise RuntimeError("preview runtime has no executions to cancel")


class LocalPipelineRuntimeFactory:
    """Compose one local Engine runtime and restore partial-run inputs."""

    def __init__(
        self,
        *,
        stage_modules: Mapping[str, object] | None = None,
        context_configurer: ContextConfigurer | None = None,
        project_root: Path | None = None,
        executor_max_concurrency: int = 1,
    ) -> None:
        self.stage_modules = (
            None if stage_modules is None else dict(stage_modules)
        )
        self.project_root = (
            Path(__file__).resolve().parents[3]
            if project_root is None
            else Path(project_root).resolve()
        )
        self.context_configurer = context_configurer
        self._uses_default_inference = context_configurer is None
        if (
            isinstance(executor_max_concurrency, bool)
            or not isinstance(executor_max_concurrency, int)
            or executor_max_concurrency < 1
        ):
            raise ValueError(
                "executor_max_concurrency must be a positive integer"
            )
        self.executor_max_concurrency = executor_max_concurrency
        if self.context_configurer is not None and not callable(
            self.context_configurer
        ):
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
        namespace = self._namespace(output_root)
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
            max_context_tokens=settings.max_context_tokens,
            context_tokenizer_model=(
                settings.context_tokenizer_model
                or sentence_transformer_tokenizer_model(settings.embed_model)
            ),
        )
        setup_logging(context.log_dir / f"run_{run_id}.log")
        context.artifact_registrar = LegacyOutputAdapter(artifact_store)
        if context.max_context_tokens is not None:
            context.context_token_counter = HuggingFaceTokenCounter(
                context.context_tokenizer_model,
            )
        if self.context_configurer is None:
            self._configure_inference(
                context,
                artifact_store,
                request.deployments,
            )
        else:
            self.context_configurer(context, artifact_store)
        model_resolver = self._model_resolver(
            context.caption_service,
            context.stt_service,
            context.diarization_service,
            context.vad_service,
            context.embedding_service,
        )
        bindings = create_legacy_pipeline_bindings(
            context,
            context.artifact_registrar,
            stage_modules=self.stage_modules,
        )
        engine = PipelineEngine(
            LocalExecutor(
                bindings,
                max_concurrency=self.executor_max_concurrency,
            ),
            run_store=run_store,
            cache_evaluator=ManifestCacheEvaluator(artifact_store),
            model_resolver=model_resolver,
        )
        return PipelineRuntime(engine=engine, artifacts=artifacts)

    def create_preview(
        self,
        request: PipelineRunRequest,
        *,
        run_id: str,
        boundary_inputs: Collection[str],
    ) -> PipelineRuntime:
        """Compose only read ports needed for cache-aware preview."""

        output_root = request.output_root.resolve()
        namespace = self._namespace(output_root)
        artifact_store = LocalArtifactStore(
            output_root,
            namespace=namespace,
            read_only=True,
        )
        run_store = LocalRunStore(
            output_root,
            artifact_store,
            read_only=True,
        )
        video = self._describe_video(request.video_path, namespace)
        artifacts = self._inspect_boundary_artifacts(
            run_store,
            artifact_store,
            run_id=run_id,
            boundary_inputs=boundary_inputs,
            video=video,
        )
        model_resolver = None
        if self._uses_default_inference:
            settings = request.settings
            model_resolver = self._model_resolver(
                create_local_caption_service(
                    settings.caption_model,
                    artifact_store,
                ),
                create_local_stt_service(
                    settings.whisper_model,
                    artifact_store,
                ),
                create_local_diarization_service(
                    settings.diarize_model,
                    artifact_store,
                    token=load_hf_token(self.project_root),
                ),
                create_local_vad_service(artifact_store),
                create_configured_embedding_service(
                    settings.embed_model,
                    deployments=request.deployments,
                ),
            )
        engine = PipelineEngine(
            _PreviewExecutor(),
            run_store=run_store,
            cache_evaluator=ManifestCacheEvaluator(artifact_store),
            model_resolver=model_resolver,
        )
        return PipelineRuntime(engine=engine, artifacts=artifacts)

    def _configure_inference(
        self,
        context: PipelineContext,
        artifact_store: LocalArtifactStore,
        deployments: InferenceDeploymentSettings,
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
        context.embedding_service = create_configured_embedding_service(
            context.embed_model,
            deployments=deployments,
        )

    @staticmethod
    def _model_resolver(*services) -> GatewayEffectiveModelResolver | None:
        gateways = {}
        for service in services:
            if service is None:
                continue
            alias = getattr(service, "alias", None)
            gateway = getattr(service, "gateway", None)
            if isinstance(alias, str) and gateway is not None:
                gateways[alias] = gateway
        if not gateways:
            return None
        return GatewayEffectiveModelResolver(gateways)

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
    def _describe_video(video_path: Path, namespace: str) -> ArtifactRef:
        resolved = video_path.resolve()
        digest = hashlib.sha256()
        size_bytes = 0
        with resolved.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                size_bytes += len(chunk)
        media_type = mimetypes.guess_type(resolved.name)[0]
        if media_type is None:
            media_type = "application/octet-stream"
        suffix = resolved.suffix.lower() or ".bin"
        return ArtifactRef(
            artifact_id="input-video",
            kind="video",
            uri=f"artifact://{namespace}/00_input/video{suffix}",
            media_type=media_type,
            size_bytes=size_bytes,
            checksum=Checksum("sha256", digest.hexdigest()),
            metadata={"source_name": resolved.name},
        )

    @staticmethod
    def _namespace(output_root: Path) -> str:
        return "local-" + hashlib.sha256(
            str(output_root).encode("utf-8")
        ).hexdigest()[:16]

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

    @staticmethod
    def _inspect_boundary_artifacts(
        run_store: LocalRunStore,
        artifact_store: LocalArtifactStore,
        *,
        run_id: str,
        boundary_inputs: Collection[str],
        video: ArtifactRef,
    ) -> dict[str, ArtifactRef]:
        artifacts = {"video": video}
        required = set(boundary_inputs) - {"video"}
        if not required:
            return artifacts
        previous_run = run_store.load_run(run_id)
        if previous_run is None:
            return artifacts
        previous_video = previous_run.input_artifacts.get("video")
        if (
            previous_video is None
            or previous_video.size_bytes != video.size_bytes
            or previous_video.checksum != video.checksum
        ):
            return artifacts
        candidates = dict(previous_run.input_artifacts)
        for stage_reference in previous_run.stages:
            manifest = run_store.load_stage(run_id, stage_reference)
            if manifest is not None:
                candidates.update(manifest.result.outputs)
        for name in sorted(required):
            ref = candidates.get(name)
            if ref is None:
                continue
            if artifact_store.verify(ref).ok:
                artifacts[name] = ref
        return artifacts
