"""Deterministic text preparation shared by indexing and retrieval."""

from .text import character_ngrams, normalize_search_text, search_terms

__all__ = ["character_ngrams", "normalize_search_text", "search_terms"]
