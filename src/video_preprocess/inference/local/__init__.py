"""Local in-process inference providers."""

from .caption import LocalCaptionProvider, create_local_caption_service
from .diarization import (
    LocalDiarizationProvider,
    create_local_diarization_service,
)
from .embedding import LocalEmbeddingProvider, get_local_embedding_service
from .stt import LocalSTTProvider, create_local_stt_service
from .vad import LocalVADProvider, create_local_vad_service

__all__ = [
    "LocalCaptionProvider",
    "LocalDiarizationProvider",
    "LocalEmbeddingProvider",
    "LocalSTTProvider",
    "LocalVADProvider",
    "create_local_caption_service",
    "create_local_diarization_service",
    "create_local_stt_service",
    "create_local_vad_service",
    "get_local_embedding_service",
]
