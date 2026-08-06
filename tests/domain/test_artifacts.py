"""Tests for versioned artifact contracts."""

import json
from pathlib import Path

import pytest

from video_preprocess.domain import (
    ArtifactRef,
    Checksum,
    ContractValidationError,
    UnsupportedSchemaVersion,
)


def make_artifact() -> ArtifactRef:
    return ArtifactRef(
        artifact_id="art_audio",
        kind="audio",
        uri="artifact://runs/run_123/04_audio/audio_16k.wav",
        media_type="audio/wav",
        size_bytes=960084,
        checksum=Checksum("SHA256", "ABC123"),
        metadata={"stage": "04_audio", "channels": 1},
    )


def test_artifact_round_trip_is_json_serializable() -> None:
    artifact = make_artifact()

    payload = artifact.to_dict()
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    restored = ArtifactRef.from_dict(json.loads(encoded))

    assert restored == artifact
    assert restored.schema_version == "1"
    assert restored.checksum.algorithm == "sha256"
    assert restored.checksum.value == "abc123"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("uri", "/tmp/audio.wav"),
        ("size_bytes", -1),
        ("artifact_id", ""),
    ],
)
def test_artifact_rejects_invalid_fields(field: str, value: object) -> None:
    data = {
        "artifact_id": "art_audio",
        "kind": "audio",
        "uri": "artifact://runs/run_123/audio.wav",
        "media_type": "audio/wav",
        "size_bytes": 10,
        "checksum": Checksum("sha256", "abc123"),
    }
    data[field] = value

    with pytest.raises(ContractValidationError) as exc_info:
        ArtifactRef(**data)

    assert exc_info.value.field == field


def test_artifact_rejects_non_json_metadata() -> None:
    with pytest.raises(ContractValidationError) as exc_info:
        ArtifactRef(
            artifact_id="art_audio",
            kind="audio",
            uri="artifact://runs/run_123/audio.wav",
            media_type="audio/wav",
            size_bytes=10,
            checksum=Checksum("sha256", "abc123"),
            metadata={"local_path": Path("/tmp/audio.wav")},
        )

    assert exc_info.value.field == "metadata.local_path"


def test_artifact_rejects_unknown_schema_version() -> None:
    payload = make_artifact().to_dict()
    payload["schema_version"] = "2"

    with pytest.raises(UnsupportedSchemaVersion):
        ArtifactRef.from_dict(payload)


def test_artifact_reader_ignores_unknown_additive_fields() -> None:
    payload = make_artifact().to_dict()
    payload["future_field"] = {"added_in_minor_version": True}

    restored = ArtifactRef.from_dict(payload)

    assert restored == make_artifact()
