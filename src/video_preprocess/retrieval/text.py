"""Unicode-safe normalization and morphology-tolerant character n-grams."""

from __future__ import annotations

import unicodedata


NORMALIZATION_VERSION = "nfkc-casefold-punctuation-space-v1"
NGRAM_VERSION = "char-2-3gram-v1"


def normalize_search_text(value: str) -> str:
    """Normalize Unicode, case, punctuation and whitespace for retrieval."""

    if not isinstance(value, str):
        raise TypeError("search text must be a string")
    normalized = unicodedata.normalize("NFKC", value).casefold()
    characters = []
    for character in normalized:
        category = unicodedata.category(character)
        characters.append(character if category[0] in {"L", "N"} else " ")
    return " ".join("".join(characters).split())


def character_ngrams(value: str, *, minimum: int = 2, maximum: int = 3) -> tuple[str, ...]:
    """Return stable unique n-grams without crossing normalized word boundaries."""

    if minimum < 1 or maximum < minimum:
        raise ValueError("invalid character n-gram range")
    normalized = normalize_search_text(value)
    grams = []
    seen = set()
    for word in normalized.split():
        for size in range(minimum, maximum + 1):
            if len(word) < size:
                continue
            for offset in range(len(word) - size + 1):
                gram = word[offset : offset + size]
                if gram not in seen:
                    seen.add(gram)
                    grams.append(gram)
    return tuple(grams)


def search_terms(value: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return normalized word terms and character n-grams for one query."""

    normalized = normalize_search_text(value)
    words = tuple(dict.fromkeys(normalized.split()))
    return words, character_ngrams(normalized)
