"""Versioned references to pipeline inputs and outputs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from ._validation import (
    JSONValue,
    SCHEMA_VERSION,
    normalize_json_object,
    require_integer,
    require_mapping,
    require_schema_version,
    require_string,
)
from .errors import ContractValidationError


@dataclass(frozen=True, slots=True)
class Checksum:
    """Content checksum used for integrity checks and cache keys."""

    algorithm: str
    value: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "algorithm",
            require_string(self.algorithm, "checksum.algorithm").lower(),
        )
        object.__setattr__(
            self,
            "value",
            require_string(self.value, "checksum.value").lower(),
        )

    def to_dict(self) -> dict[str, str]:
        return {"algorithm": self.algorithm, "value": self.value}

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "Checksum":
        mapping = require_mapping(data, "checksum")
        return cls(
            algorithm=require_string(
                mapping.get("algorithm"), "checksum.algorithm"
            ),
            value=require_string(mapping.get("value"), "checksum.value"),
        )


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """A storage-independent reference to a published artifact."""

    artifact_id: str
    kind: str
    uri: str
    media_type: str
    size_bytes: int
    checksum: Checksum
    metadata: Mapping[str, JSONValue] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "schema_version",
            require_schema_version(self.schema_version),
        )
        object.__setattr__(
            self,
            "artifact_id",
            require_string(self.artifact_id, "artifact_id"),
        )
        object.__setattr__(self, "kind", require_string(self.kind, "kind"))
        uri = require_string(self.uri, "uri")
        if not uri.startswith("artifact://"):
            raise ContractValidationError(
                "uri", "must use the artifact:// logical URI scheme"
            )
        object.__setattr__(self, "uri", uri)
        object.__setattr__(
            self,
            "media_type",
            require_string(self.media_type, "media_type"),
        )
        object.__setattr__(
            self,
            "size_bytes",
            require_integer(self.size_bytes, "size_bytes"),
        )
        if not isinstance(self.checksum, Checksum):
            raise ContractValidationError(
                "checksum", "must be a Checksum instance"
            )
        object.__setattr__(
            self,
            "metadata",
            normalize_json_object(self.metadata, "metadata"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "artifact_id": self.artifact_id,
            "kind": self.kind,
            "uri": self.uri,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "checksum": self.checksum.to_dict(),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ArtifactRef":
        mapping = require_mapping(data, "artifact")
        checksum = require_mapping(mapping.get("checksum"), "checksum")
        metadata = mapping.get("metadata", {})
        return cls(
            schema_version=require_schema_version(
                mapping.get("schema_version")
            ),
            artifact_id=require_string(
                mapping.get("artifact_id"), "artifact_id"
            ),
            kind=require_string(mapping.get("kind"), "kind"),
            uri=require_string(mapping.get("uri"), "uri"),
            media_type=require_string(
                mapping.get("media_type"), "media_type"
            ),
            size_bytes=require_integer(
                mapping.get("size_bytes"), "size_bytes"
            ),
            checksum=Checksum.from_dict(checksum),
            metadata=normalize_json_object(metadata, "metadata"),
        )

