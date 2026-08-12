"""Token counting boundary used by context assembly without model inference."""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol


class TokenCounter(Protocol):
    """Count and truncate text according to one target model tokenizer."""

    @property
    def model_name(self) -> str: ...

    def count(self, text: str) -> int: ...

    def truncate(self, text: str, max_tokens: int) -> str: ...


TokenizerLoader = Callable[[str, str | None], object]


def sentence_transformer_tokenizer_model(model_name: str) -> str:
    """Apply SentenceTransformer's default organization to short model IDs."""

    if "/" in model_name or Path(model_name).exists():
        return model_name
    return f"sentence-transformers/{model_name}"


class HuggingFaceTokenCounter:
    """Lazily load and reuse an AutoTokenizer for exact token accounting."""

    def __init__(
        self,
        model_name: str,
        *,
        revision: str | None = None,
        loader: TokenizerLoader | None = None,
    ) -> None:
        if not isinstance(model_name, str) or not model_name.strip():
            raise ValueError("model_name must be a non-empty string")
        if revision is not None and (
            not isinstance(revision, str) or not revision.strip()
        ):
            raise ValueError("revision must be non-empty or None")
        self._model_name = model_name.strip()
        self.revision = None if revision is None else revision.strip()
        self.loader = loader or _load_tokenizer
        self._tokenizer = None
        self._lock = threading.Lock()

    @property
    def model_name(self) -> str:
        return self._model_name

    def count(self, text: str) -> int:
        return len(self._encode(text))

    def truncate(self, text: str, max_tokens: int) -> str:
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int):
            raise TypeError("max_tokens must be an integer")
        if max_tokens <= 0:
            return ""
        token_ids = self._encode(text)
        if len(token_ids) <= max_tokens:
            return text
        tokenizer = self._get_tokenizer()
        decoded = tokenizer.decode(
            token_ids[:max_tokens],
            skip_special_tokens=True,
            clean_up_tokenization_spaces=False,
        )
        return str(decoded).rstrip()

    def _encode(self, text: str) -> Sequence[int]:
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        return self._get_tokenizer().encode(text, add_special_tokens=False)

    def _get_tokenizer(self):
        if self._tokenizer is not None:
            return self._tokenizer
        with self._lock:
            if self._tokenizer is None:
                self._tokenizer = self.loader(
                    self._model_name,
                    self.revision,
                )
        return self._tokenizer


def _load_tokenizer(model_name: str, revision: str | None):
    from transformers import AutoTokenizer

    return AutoTokenizer.from_pretrained(model_name, revision=revision)
