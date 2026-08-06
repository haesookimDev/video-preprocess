"""JSON manifest Run Store backed by the local filesystem."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

from video_preprocess.domain import (
    ContractValidationError,
    RunManifest,
    RunStatus,
    StageAttemptRef,
    StageManifest,
    StageStatus,
)

from ._atomic import atomic_write_json
from .artifacts import ArtifactStore
from .errors import (
    ArtifactIntegrityError,
    IncompleteRunError,
    ManifestFormatError,
    StorageError,
)


def _id_segment(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return "id-" + quote(value, safe="-._~")


class LocalRunStore:
    """Persists run and Stage manifests after artifact verification."""

    def __init__(self, root: Path, artifacts: ArtifactStore) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest_root = self.root / "_manifests"
        if self.manifest_root.is_symlink():
            raise ManifestFormatError(
                "reserved _manifests directory must not be a symbolic link"
            )
        self.manifest_root.mkdir(parents=True, exist_ok=True)
        self.artifacts = artifacts

    def save_run(self, manifest: RunManifest) -> None:
        if not isinstance(manifest, RunManifest):
            raise TypeError("manifest must be a RunManifest")
        if manifest.status is RunStatus.SUCCEEDED:
            incomplete = [
                stage
                for stage in manifest.stages
                if not self.is_stage_complete(manifest.run_id, stage)
            ]
            if incomplete:
                formatted = ", ".join(
                    f"{stage.stage_run_id}@{stage.attempt}"
                    for stage in incomplete
                )
                raise IncompleteRunError(
                    f"succeeded run has incomplete stages: {formatted}"
                )
        atomic_write_json(self._run_path(manifest.run_id), manifest.to_dict())

    def load_run(self, run_id: str) -> RunManifest | None:
        path = self._run_path(run_id)
        payload = self._read_json(path)
        if payload is None:
            return None
        try:
            manifest = RunManifest.from_dict(payload)
        except (ContractValidationError, TypeError, ValueError) as exc:
            raise ManifestFormatError(f"invalid run manifest: {path}") from exc
        if manifest.run_id != run_id:
            raise ManifestFormatError(
                f"run manifest ID does not match path: {path}"
            )
        return manifest

    def save_stage(self, manifest: StageManifest) -> None:
        if not isinstance(manifest, StageManifest):
            raise TypeError("manifest must be a StageManifest")
        for output_name, artifact_ref in manifest.result.outputs.items():
            verification = self.artifacts.verify(artifact_ref)
            if not verification.ok:
                raise ArtifactIntegrityError(
                    f"output {output_name!r} is missing or corrupt: "
                    f"{artifact_ref.uri}"
                )
        path = self._stage_path(
            manifest.task.run_id,
            manifest.reference,
        )
        atomic_write_json(path, manifest.to_dict())

    def load_stage(
        self,
        run_id: str,
        stage: StageAttemptRef,
    ) -> StageManifest | None:
        if not isinstance(stage, StageAttemptRef):
            raise TypeError("stage must be a StageAttemptRef")
        path = self._stage_path(run_id, stage)
        payload = self._read_json(path)
        if payload is None:
            return None
        try:
            manifest = StageManifest.from_dict(payload)
        except (ContractValidationError, TypeError, ValueError) as exc:
            raise ManifestFormatError(f"invalid Stage manifest: {path}") from exc
        if manifest.task.run_id != run_id or manifest.reference != stage:
            raise ManifestFormatError(
                f"Stage manifest identifiers do not match path: {path}"
            )
        return manifest

    def is_stage_complete(
        self,
        run_id: str,
        stage: StageAttemptRef,
    ) -> bool:
        manifest = self.load_stage(run_id, stage)
        if manifest is None or manifest.result.status not in {
            StageStatus.SUCCEEDED,
            StageStatus.SKIPPED,
        }:
            return False
        for artifact_ref in manifest.result.outputs.values():
            try:
                verification = self.artifacts.verify(artifact_ref)
            except StorageError:
                return False
            if not verification.ok:
                return False
        return True

    def _run_directory(self, run_id: str) -> Path:
        return self.manifest_root / _id_segment(run_id, "run_id")

    def _run_path(self, run_id: str) -> Path:
        return self._safe_manifest_path(
            self._run_directory(run_id) / "run.json"
        )

    def _stage_path(
        self,
        run_id: str,
        stage: StageAttemptRef,
    ) -> Path:
        stage_directory = (
            self._run_directory(run_id)
            / "stages"
            / _id_segment(stage.stage_run_id, "stage_run_id")
        )
        return self._safe_manifest_path(
            stage_directory / f"attempt-{stage.attempt:04d}.json"
        )

    def _safe_manifest_path(self, path: Path) -> Path:
        try:
            path.resolve(strict=False).relative_to(
                self.manifest_root.resolve()
            )
        except ValueError as exc:
            raise ManifestFormatError(
                "manifest path escapes the configured root"
            ) from exc
        return path

    @staticmethod
    def _read_json(path: Path) -> dict[str, object] | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ManifestFormatError(f"cannot read manifest: {path}") from exc
        if not isinstance(payload, dict):
            raise ManifestFormatError(f"manifest must be an object: {path}")
        return payload
