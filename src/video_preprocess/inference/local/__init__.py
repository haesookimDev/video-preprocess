"""Local in-process inference providers."""

from .embedding import LocalEmbeddingProvider, get_local_embedding_service

__all__ = ["LocalEmbeddingProvider", "get_local_embedding_service"]

