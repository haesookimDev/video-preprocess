"""Compatibility adapter for unversioned JSON in existing output trees."""

from __future__ import annotations

import json
from collections.abc import Mapping

from video_preprocess.domain import ArtifactRef, ContractValidationError
from video_preprocess.domain._validation import (
    JSONValue,
    normalize_json,
    normalize_json_object,
)

from .errors import LegacyArtifactFormatError
from .local_artifacts import LocalArtifactStore


class LegacyOutputAdapter:
    """Tags and reads current MVP JSON without rewriting its contents."""

    def __init__(self, store: LocalArtifactStore) -> None:
        self.store = store

    def register_json(
        self,
        relative_path: str,
        *,
        artifact_id: str,
        metadata: Mapping[str, JSONValue] | None = None,
    ) -> ArtifactRef:
        normalized_metadata = normalize_json_object(
            {} if metadata is None else metadata,
            "metadata",
        )
        normalized_metadata["legacy_schema"] = "v1"
        return self.store.register_existing(
            relative_path,
            artifact_id=artifact_id,
            kind="json",
            media_type="application/json",
            metadata=normalized_metadata,
        )

    def load_json(self, artifact_ref: ArtifactRef) -> JSONValue:
        try:
            with self.store.open(artifact_ref) as handle:
                payload = json.load(handle)
            return normalize_json(payload, "legacy_artifact")
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ContractValidationError,
        ) as exc:
            raise LegacyArtifactFormatError(
                f"cannot decode legacy JSON: {artifact_ref.uri}"
            ) from exc
