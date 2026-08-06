"""Artifact Store port and transport-neutral helper values."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Protocol

from video_preprocess.domain import ArtifactRef, Checksum
from video_preprocess.domain._validation import JSONValue, normalize_json_object


@dataclass(frozen=True, slots=True)
class PendingArtifact:
    """Opaque unpublished artifact returned by ``ArtifactStore.put``."""

    token: str
    artifact_id: str
    relative_path: str
    kind: str
    media_type: str
    size_bytes: int
    checksum: Checksum
    metadata: Mapping[str, JSONValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in (
            "token",
            "artifact_id",
            "relative_path",
            "kind",
            "media_type",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or self.size_bytes < 0
        ):
            raise ValueError("size_bytes must be a non-negative integer")
        if not isinstance(self.checksum, Checksum):
            raise TypeError("checksum must be a Checksum")
        object.__setattr__(
            self,
            "metadata",
            normalize_json_object(self.metadata, "metadata"),
        )


@dataclass(frozen=True, slots=True)
class ArtifactVerification:
    """Integrity comparison between an ArtifactRef and stored bytes."""

    exists: bool
    expected_size_bytes: int
    actual_size_bytes: int | None
    expected_checksum: Checksum
    actual_checksum: Checksum | None

    @property
    def size_matches(self) -> bool:
        return self.actual_size_bytes == self.expected_size_bytes

    @property
    def checksum_matches(self) -> bool:
        return self.actual_checksum == self.expected_checksum

    @property
    def ok(self) -> bool:
        return self.exists and self.size_matches and self.checksum_matches


class ArtifactStore(Protocol):
    """Storage-independent lifecycle for binary pipeline artifacts."""

    def put(
        self,
        stream: BinaryIO,
        *,
        artifact_id: str,
        relative_path: str,
        kind: str,
        media_type: str,
        metadata: Mapping[str, JSONValue] | None = None,
    ) -> PendingArtifact: ...

    def publish(self, pending: PendingArtifact) -> ArtifactRef: ...

    def discard(self, pending: PendingArtifact) -> None: ...

    def open(self, artifact_ref: ArtifactRef) -> BinaryIO: ...

    def materialize(
        self,
        artifact_ref: ArtifactRef,
        workspace: Path,
    ) -> Path: ...

    def exists(self, artifact_ref: ArtifactRef) -> bool: ...

    def verify(self, artifact_ref: ArtifactRef) -> ArtifactVerification: ...
