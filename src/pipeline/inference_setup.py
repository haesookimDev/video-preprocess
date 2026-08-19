"""Compatibility composition root for provider-backed MVP stages."""

import hashlib
from pathlib import Path

from video_preprocess.inference.local import (
    create_local_caption_service,
    create_local_diarization_service,
    create_local_ocr_service,
    create_local_stt_service,
    create_local_vad_service,
)
from video_preprocess.storage import LocalArtifactStore, LegacyOutputAdapter

from .context import PipelineContext
from .preflight import load_hf_token


def configure_local_inference(ctx: PipelineContext) -> None:
    """Inject local inference services without loading model weights."""

    dependencies = (
        ctx.caption_service,
        ctx.stt_service,
        ctx.ocr_service,
        ctx.diarization_service,
        ctx.vad_service,
        ctx.artifact_registrar,
    )
    if all(dependency is not None for dependency in dependencies):
        return
    if any(dependency is not None for dependency in dependencies):
        raise ValueError(
            "caption_service, ocr_service, stt_service, "
            "diarization_service, vad_service, "
            "and artifact_registrar "
            "must be configured together"
        )

    namespace_hash = hashlib.sha256(
        str(ctx.out_root).encode("utf-8")
    ).hexdigest()[:16]
    artifact_store = LocalArtifactStore(
        ctx.out_root,
        namespace=f"legacy-{namespace_hash}",
    )
    ctx.artifact_registrar = LegacyOutputAdapter(artifact_store)
    ctx.caption_service = create_local_caption_service(
        ctx.caption_model,
        artifact_store,
    )
    ctx.ocr_service = create_local_ocr_service(
        artifact_store,
        model_name=ctx.ocr_model,
    )
    ctx.stt_service = create_local_stt_service(
        ctx.whisper_model,
        artifact_store,
    )
    project_root = Path(__file__).resolve().parents[2]
    ctx.diarization_service = create_local_diarization_service(
        ctx.diarize_model,
        artifact_store,
        token=load_hf_token(project_root),
    )
    ctx.vad_service = create_local_vad_service(artifact_store)
