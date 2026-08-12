"""Read-only effective revision probes for local model providers."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path


_IMMUTABLE_REVISION = re.compile(r"^[0-9a-fA-F]{40}$")
_OFFLINE_TRUE = {"1", "on", "true", "yes"}


def resolve_hf_cache_revision(
    repo_id: str,
    filename: str,
    revision: str | None,
) -> str | None:
    """Resolve a snapshot without network access or cache mutation."""

    if Path(repo_id).is_dir():
        return None
    if not _immutable_revision(revision) and not _offline_mode():
        return None
    try:
        from huggingface_hub import try_to_load_from_cache

        cached_path = try_to_load_from_cache(
            repo_id=repo_id,
            filename=filename,
            revision=revision,
        )
    except Exception:
        return None
    if not isinstance(cached_path, str):
        return None
    return snapshot_revision(cached_path)


def faster_whisper_repo_id(model_name: str) -> str:
    """Map a faster-whisper size alias to its Hub repository."""

    try:
        from faster_whisper.utils import _MODELS
    except Exception:
        return model_name
    return _MODELS.get(model_name, model_name)


def sentence_transformer_repo_id(model_name: str) -> str:
    """Apply SentenceTransformer's default Hub organization."""

    if "/" in model_name or Path(model_name).exists():
        return model_name
    return f"sentence-transformers/{model_name}"


def resolve_vad_asset_revision() -> str | None:
    """Hash the packaged Silero ONNX asset without loading the model."""

    try:
        from faster_whisper.utils import get_assets_path

        asset_path = Path(get_assets_path()) / "silero_vad_v6.onnx"
        digest = hashlib.sha256()
        with asset_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except (ImportError, OSError):
        return None
    return f"sha256:{digest.hexdigest()}"


def snapshot_revision(path: str) -> str | None:
    """Extract an immutable commit from a lexical Hub snapshot path."""

    parts = Path(path).parts
    for index, part in enumerate(parts[:-1]):
        if part == "snapshots":
            candidate = parts[index + 1]
            if candidate:
                return candidate
    return None


def _immutable_revision(revision: str | None) -> bool:
    return (
        revision is not None
        and _IMMUTABLE_REVISION.fullmatch(revision) is not None
    )


def _offline_mode() -> bool:
    return any(
        os.environ.get(name, "").strip().lower() in _OFFLINE_TRUE
        for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")
    )
