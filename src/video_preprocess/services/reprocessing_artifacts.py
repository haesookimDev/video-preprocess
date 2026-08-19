"""Verified source Artifact import for immutable derived reprocessing runs."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from video_preprocess.domain import ArtifactRef
from video_preprocess.storage import ArtifactStore, PendingArtifact

from .reprocessing import ReprocessingPlan, ReprocessingServiceError


SOURCE_IMPORT_POLICY = "verified-derived-copy-v1"
SOURCE_MANIFEST_CONTRACT = "reprocessing-source-manifest-v1"
SOURCE_ARTIFACT_PATHS = {
    "metadata": "00_source/01_probe/metadata.json",
    "scenes": "00_source/02_scenes/scenes.json",
    "keyframes": "00_source/03_keyframes/keyframes.json",
    "keyframe_images": "00_source/03_keyframes/keyframe_images.zip",
    "embedded_text": "00_source/04_embedded_text/embedded_text.json",
    "audio_events": "00_source/05_audio_events/audio_events.json",
    "transcript": "00_source/06_stt/transcript.json",
    "diarization": "00_source/07_diarize/diarization.json",
    "captions": "00_source/08_captions/captions.json",
    "ocr": "00_source/08_ocr/ocr.json",
    "timeline": "00_source/09_timeline/timeline.json",
    "search_index": "00_source/10_index/index.db",
}
SOURCE_BOUNDARY_ALIASES = {
    "keyframes": "source_keyframes",
    "keyframe_images": "source_keyframe_images",
    "captions": "source_captions",
    "ocr": "source_ocr",
}


class ReprocessingArtifactImportError(ReprocessingServiceError):
    """Source artifacts could not be verified and imported safely."""


@dataclass(frozen=True, slots=True)
class ImportedReprocessingSource:
    """Published source snapshot and execution boundary aliases."""

    source_run_id: str
    artifacts: Mapping[str, ArtifactRef]
    boundary_inputs: Mapping[str, ArtifactRef]
    manifest: ArtifactRef


class ReprocessingArtifactImporter:
    """Copy one plan's verified source snapshot into a fresh target Store."""

    def __init__(
        self,
        source_store: ArtifactStore,
        target_store: ArtifactStore,
    ) -> None:
        for field_name, store in (
            ("source_store", source_store),
            ("target_store", target_store),
        ):
            for method_name in ("verify", "open", "put", "publish", "discard"):
                if not callable(getattr(store, method_name, None)):
                    raise TypeError(
                        f"{field_name} must implement ArtifactStore.{method_name}"
                    )
        self.source_store = source_store
        self.target_store = target_store
        source_root = getattr(source_store, "root", None)
        target_root = getattr(target_store, "root", None)
        source_namespace = getattr(source_store, "namespace", None)
        target_namespace = getattr(target_store, "namespace", None)
        if (
            source_store is target_store
            or (
                source_root is not None
                and target_root is not None
                and source_root == target_root
            )
            or (
                source_namespace is not None
                and target_namespace is not None
                and source_namespace == target_namespace
            )
        ):
            raise ValueError(
                "source_store and target_store must use distinct roots and "
                "namespaces"
            )

    def import_plan(self, plan: ReprocessingPlan) -> ImportedReprocessingSource:
        if not isinstance(plan, ReprocessingPlan):
            raise TypeError("plan must be a ReprocessingPlan")
        expected = set(SOURCE_ARTIFACT_PATHS) | {"video"}
        if set(plan.source_artifacts) != expected:
            missing = sorted(expected - set(plan.source_artifacts))
            extra = sorted(set(plan.source_artifacts) - expected)
            details = []
            if missing:
                details.append("missing=" + ",".join(missing))
            if extra:
                details.append("extra=" + ",".join(extra))
            raise ReprocessingArtifactImportError(
                "source snapshot does not match the import contract: "
                + " ".join(details)
            )

        ordered = tuple(sorted(plan.source_artifacts.items()))
        for name, ref in ordered:
            try:
                verification = self.source_store.verify(ref)
            except Exception as exc:
                raise ReprocessingArtifactImportError(
                    f"source artifact verification failed: {name}"
                ) from exc
            if not verification.ok:
                raise ReprocessingArtifactImportError(
                    f"source artifact integrity check failed: {name}"
                )

        pending: list[tuple[str, PendingArtifact]] = []
        try:
            for name, ref in ordered:
                relative_path = self._target_path(name, ref)
                with self.source_store.open(ref) as stream:
                    value = self.target_store.put(
                        stream,
                        artifact_id=f"source:{plan.source_run_id}:{name}",
                        relative_path=relative_path,
                        kind=ref.kind,
                        media_type=ref.media_type,
                        metadata={
                            "source_run_id": plan.source_run_id,
                            "source_artifact_id": ref.artifact_id,
                            "source_uri": ref.uri,
                            "source_checksum": ref.checksum.to_dict(),
                            "plan_fingerprint": plan.plan_fingerprint,
                            "import_policy": SOURCE_IMPORT_POLICY,
                        },
                    )
                pending.append((name, value))
                if (
                    value.size_bytes != ref.size_bytes
                    or value.checksum != ref.checksum
                ):
                    raise ReprocessingArtifactImportError(
                        f"source artifact changed while importing: {name}"
                    )
        except BaseException:
            self._discard_pending(pending)
            raise

        published = {}
        for position, (name, value) in enumerate(pending):
            try:
                published[name] = self.target_store.publish(value)
            except Exception as exc:
                self._discard_pending(pending[position + 1:])
                raise ReprocessingArtifactImportError(
                    f"source artifact publication failed: {name}"
                ) from exc
        boundary_inputs = {
            SOURCE_BOUNDARY_ALIASES.get(name, name): ref
            for name, ref in published.items()
            if name not in {"timeline", "search_index"}
        }
        manifest_payload = {
            "contract": SOURCE_MANIFEST_CONTRACT,
            "source_run_id": plan.source_run_id,
            "plan_id": plan.plan_id,
            "plan_fingerprint": plan.plan_fingerprint,
            "import_policy": SOURCE_IMPORT_POLICY,
            "artifacts": {
                name: ref.to_dict()
                for name, ref in sorted(published.items())
            },
            "boundary_inputs": {
                name: ref.to_dict()
                for name, ref in sorted(boundary_inputs.items())
            },
        }
        encoded = json.dumps(
            manifest_payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ).encode("utf-8") + b"\n"
        try:
            manifest_pending = self.target_store.put(
                BytesIO(encoded),
                artifact_id=f"source:{plan.source_run_id}:manifest",
                relative_path="00_source/source_manifest.json",
                kind="json",
                media_type="application/json",
                metadata={
                    "source_run_id": plan.source_run_id,
                    "plan_fingerprint": plan.plan_fingerprint,
                    "import_policy": SOURCE_IMPORT_POLICY,
                },
            )
            manifest = self.target_store.publish(manifest_pending)
        except Exception as exc:
            raise ReprocessingArtifactImportError(
                "source import manifest publication failed"
            ) from exc
        return ImportedReprocessingSource(
            source_run_id=plan.source_run_id,
            artifacts=published,
            boundary_inputs=boundary_inputs,
            manifest=manifest,
        )

    def _discard_pending(
        self,
        pending: list[tuple[str, PendingArtifact]],
    ) -> None:
        for _, value in reversed(pending):
            try:
                self.target_store.discard(value)
            except Exception:
                pass

    @staticmethod
    def _target_path(name: str, ref: ArtifactRef) -> str:
        if name != "video":
            return SOURCE_ARTIFACT_PATHS[name]
        path = PurePosixPath(unquote(urlsplit(ref.uri).path))
        suffix = path.suffix.lower()
        if not suffix or len(suffix) > 16 or not suffix[1:].isalnum():
            suffix = ".bin"
        return f"00_source/00_input/video{suffix}"


__all__ = [
    "ImportedReprocessingSource",
    "ReprocessingArtifactImportError",
    "ReprocessingArtifactImporter",
    "SOURCE_ARTIFACT_PATHS",
    "SOURCE_BOUNDARY_ALIASES",
    "SOURCE_IMPORT_POLICY",
    "SOURCE_MANIFEST_CONTRACT",
]
