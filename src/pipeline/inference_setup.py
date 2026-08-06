"""Compatibility composition root for provider-backed MVP stages."""

import hashlib

from video_preprocess.inference.local import create_local_caption_service
from video_preprocess.storage import LocalArtifactStore, LegacyOutputAdapter

from .context import PipelineContext


def configure_local_inference(ctx: PipelineContext) -> None:
    """Inject local inference services without loading model weights."""

    if ctx.caption_service is not None and ctx.artifact_registrar is not None:
        return
    if ctx.caption_service is not None or ctx.artifact_registrar is not None:
        raise ValueError(
            "caption_service and artifact_registrar must be configured together"
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
