"""Local in-process inference providers."""

from .caption import LocalCaptionProvider, create_local_caption_service
from .embedding import LocalEmbeddingProvider, get_local_embedding_service

__all__ = [
    "LocalCaptionProvider",
    "LocalEmbeddingProvider",
    "create_local_caption_service",
    "get_local_embedding_service",
]
