"""Tests for verified source imports into immutable derived workspaces."""

import hashlib
import json
from io import BytesIO
from pathlib import Path

import pytest

from video_preprocess.services import (
    ReprocessingArtifactImportError,
    ReprocessingArtifactImporter,
    ReprocessingCandidate,
    ReprocessingPlan,
    ReprocessingStageContract,
    VISUAL_DETAIL_V1,
)
from video_preprocess.storage import LocalArtifactStore


SOURCE_NAMES = (
    "audio_events",
    "captions",
    "diarization",
    "embedded_text",
    "keyframe_images",
    "keyframes",
    "metadata",
    "ocr",
    "scenes",
    "search_index",
    "timeline",
    "transcript",
    "video",
)


def _source_store(tmp_path: Path):
    root = tmp_path / "parent"
    store = LocalArtifactStore(root, namespace="parent-run")
    artifacts = {}
    for name in SOURCE_NAMES:
        payload = f"immutable-{name}".encode()
        suffix = ".mp4" if name == "video" else ".bin"
        pending = store.put(
            stream=BytesIO(payload),
            artifact_id=f"parent:{name}",
            relative_path=f"artifacts/{name}{suffix}",
            kind="video" if name == "video" else "artifact",
            media_type=(
                "video/mp4"
                if name == "video"
                else "application/octet-stream"
            ),
        )
        artifacts[name] = store.publish(pending)
    return root, store, artifacts


def _plan(artifacts) -> ReprocessingPlan:
    stage_versions = {
        "03_keyframes": "1.4.0",
        "08_captions": "1.4.0",
        "08_ocr": "1.1.0",
        "09_timeline": "1.6.0",
        "10_index": "1.4.0",
        "11_context": "1.5.0",
    }
    return ReprocessingPlan(
        plan_id="reprocess_plan_123",
        request_fingerprint="request-fingerprint",
        plan_fingerprint="plan-fingerprint",
        source_run_id="parent-run",
        query="dashboard",
        normalized_query="dashboard",
        profile=VISUAL_DETAIL_V1,
        candidates=(
            ReprocessingCandidate(
                rank=1,
                scene_id=2,
                start_sec=10,
                end_sec=20,
                score=0.9,
                reasons=("semantic",),
            ),
        ),
        stages=tuple(
            ReprocessingStageContract(
                name=name,
                version=version,
                scope=(
                    "selected-scenes"
                    if name in {"03_keyframes", "08_captions", "08_ocr"}
                    else "full-materialization"
                ),
            )
            for name, version in stage_versions.items()
        ),
        boundary_inputs=(
            "audio_events",
            "diarization",
            "embedded_text",
            "metadata",
            "scenes",
            "source_captions",
            "source_keyframe_images",
            "source_keyframes",
            "source_ocr",
            "transcript",
            "video",
        ),
        source_artifacts=artifacts,
        pending_capabilities=("derived-run-application-runtime-v1",),
    )


def _snapshot(root: Path) -> dict[str, tuple[int, str]]:
    return {
        path.relative_to(root).as_posix(): (
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in root.rglob("*")
        if path.is_file()
    }


def test_import_verifies_source_and_publishes_derived_snapshot(
    tmp_path: Path,
) -> None:
    parent_root, source_store, artifacts = _source_store(tmp_path)
    before = _snapshot(parent_root)
    target_root = tmp_path / "derived"
    target_store = LocalArtifactStore(target_root, namespace="derived-run")

    imported = ReprocessingArtifactImporter(
        source_store,
        target_store,
    ).import_plan(_plan(artifacts))

    assert set(imported.artifacts) == set(SOURCE_NAMES)
    assert set(imported.boundary_inputs) == {
        "audio_events",
        "diarization",
        "embedded_text",
        "metadata",
        "scenes",
        "source_captions",
        "source_keyframe_images",
        "source_keyframes",
        "source_ocr",
        "transcript",
        "video",
    }
    assert all(
        target_store.verify(ref).ok for ref in imported.artifacts.values()
    )
    with target_store.open(imported.manifest) as handle:
        manifest = json.load(handle)
    assert manifest["contract"] == "reprocessing-source-manifest-v1"
    assert manifest["source_run_id"] == "parent-run"
    assert "/Users/" not in str(manifest)
    assert _snapshot(parent_root) == before


def test_import_rejects_corrupt_source_before_target_publication(
    tmp_path: Path,
) -> None:
    _, source_store, artifacts = _source_store(tmp_path)
    source_store.root.joinpath("artifacts", "timeline.bin").write_bytes(
        b"corrupt"
    )
    target_root = tmp_path / "derived"
    target_store = LocalArtifactStore(target_root, namespace="derived-run")

    with pytest.raises(
        ReprocessingArtifactImportError,
        match="timeline",
    ):
        ReprocessingArtifactImporter(
            source_store,
            target_store,
        ).import_plan(_plan(artifacts))

    assert not (target_root / "00_source").exists()


def test_import_rejects_parent_store_as_derived_target(tmp_path: Path) -> None:
    _, source_store, _ = _source_store(tmp_path)

    with pytest.raises(ValueError, match="distinct roots and namespaces"):
        ReprocessingArtifactImporter(source_store, source_store)
