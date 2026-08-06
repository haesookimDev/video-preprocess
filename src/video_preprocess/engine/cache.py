"""Manifest-based cache keys and reusable Stage result decisions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from video_preprocess.domain import (
    ArtifactRef,
    ModelExecution,
    StageManifest,
    StageStatus,
    StageTask,
)

from .errors import EngineInputError

if TYPE_CHECKING:
    from video_preprocess.storage.artifacts import ArtifactStore


class CacheStatus(str, Enum):
    """Outcome of evaluating one planned Stage against a manifest."""

    HIT = "hit"
    MISS = "miss"
    FORCED = "forced"


class CacheMissReason(str, Enum):
    """Stable reasons suitable for logs, metrics, and dry-run output."""

    FORCE_REQUESTED = "FORCE_REQUESTED"
    MANIFEST_NOT_FOUND = "MANIFEST_NOT_FOUND"
    TASK_SCHEMA_CHANGED = "TASK_SCHEMA_CHANGED"
    STAGE_CHANGED = "STAGE_CHANGED"
    STAGE_VERSION_CHANGED = "STAGE_VERSION_CHANGED"
    INPUTS_CHANGED = "INPUTS_CHANGED"
    CONFIG_CHANGED = "CONFIG_CHANGED"
    MODEL_BINDINGS_CHANGED = "MODEL_BINDINGS_CHANGED"
    CACHE_KEY_MISSING = "CACHE_KEY_MISSING"
    CACHE_KEY_MISMATCH = "CACHE_KEY_MISMATCH"
    RESULT_NOT_CACHEABLE = "RESULT_NOT_CACHEABLE"
    SKIPPED_RECHECK_REQUIRED = "SKIPPED_RECHECK_REQUIRED"
    EFFECTIVE_MODELS_UNAVAILABLE = "EFFECTIVE_MODELS_UNAVAILABLE"
    EFFECTIVE_MODELS_CHANGED = "EFFECTIVE_MODELS_CHANGED"
    INPUT_NOT_FOUND = "INPUT_NOT_FOUND"
    INPUT_SIZE_MISMATCH = "INPUT_SIZE_MISMATCH"
    INPUT_CHECKSUM_MISMATCH = "INPUT_CHECKSUM_MISMATCH"
    INPUT_VERIFICATION_FAILED = "INPUT_VERIFICATION_FAILED"
    OUTPUT_NOT_FOUND = "OUTPUT_NOT_FOUND"
    OUTPUT_SIZE_MISMATCH = "OUTPUT_SIZE_MISMATCH"
    OUTPUT_CHECKSUM_MISMATCH = "OUTPUT_CHECKSUM_MISMATCH"
    OUTPUT_VERIFICATION_FAILED = "OUTPUT_VERIFICATION_FAILED"


@dataclass(frozen=True, slots=True)
class CacheMiss:
    """One classified reason that prevents reuse."""

    reason: CacheMissReason
    subject: str | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.reason, CacheMissReason):
            raise TypeError("reason must be a CacheMissReason")
        for field_name in ("subject", "detail"):
            value = getattr(self, field_name)
            if value is not None and (
                not isinstance(value, str) or not value.strip()
            ):
                raise ValueError(f"{field_name} must be non-empty or None")


@dataclass(frozen=True, slots=True)
class CacheDecision:
    """Cache disposition plus all deterministic invalidation reasons."""

    status: CacheStatus
    cache_key: str
    misses: Sequence[CacheMiss] = ()
    manifest: StageManifest | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, CacheStatus):
            raise TypeError("status must be a CacheStatus")
        if not isinstance(self.cache_key, str) or not self.cache_key.strip():
            raise ValueError("cache_key must be a non-empty string")
        misses = tuple(self.misses)
        if not all(isinstance(miss, CacheMiss) for miss in misses):
            raise TypeError("misses must contain CacheMiss values")
        object.__setattr__(self, "misses", misses)
        if self.manifest is not None and not isinstance(
            self.manifest,
            StageManifest,
        ):
            raise TypeError("manifest must be a StageManifest or None")
        if self.status is CacheStatus.HIT:
            if misses or self.manifest is None:
                raise ValueError("a cache hit requires a manifest and no misses")
        elif not misses:
            raise ValueError("a cache miss or force decision requires a reason")

    @property
    def hit(self) -> bool:
        return self.status is CacheStatus.HIT

    @property
    def outputs(self) -> dict[str, ArtifactRef]:
        if not self.hit or self.manifest is None:
            return {}
        return dict(self.manifest.result.outputs)


def compute_stage_cache_key(task: StageTask) -> str:
    """Hash only Stage result semantics, excluding run-local identity."""

    if not isinstance(task, StageTask):
        raise TypeError("task must be a StageTask")
    payload = {
        "cache_schema": "1",
        "task_schema": task.schema_version,
        "stage": task.stage,
        "stage_version": task.stage_version,
        "inputs": _input_fingerprint(task.inputs),
        "config": dict(task.config),
        "model_bindings": dict(task.model_bindings),
    }
    digest = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return f"stage-cache-v1:{digest}"


class ManifestCacheEvaluator:
    """Validate a Stage manifest and all referenced bytes before reuse."""

    def __init__(self, artifact_store: ArtifactStore) -> None:
        if not callable(getattr(artifact_store, "verify", None)):
            raise TypeError("artifact_store must implement verify")
        self.artifact_store = artifact_store

    def evaluate(
        self,
        task: StageTask,
        manifest: StageManifest | None,
        *,
        expected_models: Sequence[ModelExecution] | None = None,
        force: bool = False,
    ) -> CacheDecision:
        if not isinstance(task, StageTask):
            raise TypeError("task must be a StageTask")
        if manifest is not None and not isinstance(manifest, StageManifest):
            raise TypeError("manifest must be a StageManifest or None")
        if not isinstance(force, bool):
            raise TypeError("force must be a boolean")
        normalized_models = self._normalize_expected_models(
            task,
            expected_models,
        )
        cache_key = compute_stage_cache_key(task)
        if force:
            return CacheDecision(
                status=CacheStatus.FORCED,
                cache_key=cache_key,
                misses=(CacheMiss(CacheMissReason.FORCE_REQUESTED),),
                manifest=manifest,
            )
        if manifest is None:
            return CacheDecision(
                status=CacheStatus.MISS,
                cache_key=cache_key,
                misses=(CacheMiss(CacheMissReason.MANIFEST_NOT_FOUND),),
            )

        misses: list[CacheMiss] = []
        previous = manifest.task
        if previous.schema_version != task.schema_version:
            misses.append(CacheMiss(CacheMissReason.TASK_SCHEMA_CHANGED))
        if previous.stage != task.stage:
            misses.append(CacheMiss(CacheMissReason.STAGE_CHANGED))
        if previous.stage_version != task.stage_version:
            misses.append(CacheMiss(CacheMissReason.STAGE_VERSION_CHANGED))
        if _input_fingerprint(previous.inputs) != _input_fingerprint(
            task.inputs
        ):
            misses.append(CacheMiss(CacheMissReason.INPUTS_CHANGED))
        if dict(previous.config) != dict(task.config):
            misses.append(CacheMiss(CacheMissReason.CONFIG_CHANGED))
        if dict(previous.model_bindings) != dict(task.model_bindings):
            misses.append(CacheMiss(CacheMissReason.MODEL_BINDINGS_CHANGED))
        if manifest.cache_key is None:
            misses.append(CacheMiss(CacheMissReason.CACHE_KEY_MISSING))
        elif manifest.cache_key != cache_key:
            misses.append(CacheMiss(CacheMissReason.CACHE_KEY_MISMATCH))

        if manifest.result.status is StageStatus.SKIPPED:
            misses.append(
                CacheMiss(CacheMissReason.SKIPPED_RECHECK_REQUIRED)
            )
        elif manifest.result.status is not StageStatus.SUCCEEDED:
            misses.append(CacheMiss(CacheMissReason.RESULT_NOT_CACHEABLE))

        if task.model_bindings and normalized_models is None:
            misses.append(
                CacheMiss(CacheMissReason.EFFECTIVE_MODELS_UNAVAILABLE)
            )
        elif normalized_models is not None and self._model_fingerprint(
            manifest.result.models
        ) != self._model_fingerprint(normalized_models):
            misses.append(
                CacheMiss(CacheMissReason.EFFECTIVE_MODELS_CHANGED)
            )

        if manifest.result.status is StageStatus.SUCCEEDED:
            misses.extend(self._verify_artifacts(task.inputs, input_ref=True))
            misses.extend(
                self._verify_artifacts(
                    manifest.result.outputs,
                    input_ref=False,
                )
            )

        return CacheDecision(
            status=CacheStatus.MISS if misses else CacheStatus.HIT,
            cache_key=cache_key,
            misses=tuple(misses),
            manifest=manifest,
        )

    @staticmethod
    def _normalize_expected_models(
        task: StageTask,
        expected_models: Sequence[ModelExecution] | None,
    ) -> tuple[ModelExecution, ...] | None:
        if expected_models is None:
            return None if task.model_bindings else ()
        if isinstance(expected_models, (str, bytes)) or not isinstance(
            expected_models,
            Sequence,
        ):
            raise TypeError("expected_models must be a sequence or None")
        normalized = tuple(expected_models)
        if not all(isinstance(model, ModelExecution) for model in normalized):
            raise TypeError("expected_models must contain ModelExecution values")
        actual_slots = {model.slot for model in normalized}
        expected_slots = set(task.model_bindings)
        if len(actual_slots) != len(normalized):
            raise EngineInputError("expected_models contains duplicate slots")
        if actual_slots != expected_slots:
            raise EngineInputError(
                "expected_models slots must match task model bindings"
            )
        return normalized

    @staticmethod
    def _model_fingerprint(
        models: Sequence[ModelExecution],
    ) -> tuple[tuple[str, str, str, str, str | None], ...]:
        return tuple(
            sorted(
                (
                    model.slot,
                    model.provider,
                    model.model,
                    model.revision,
                    model.runtime,
                )
                for model in models
            )
        )

    def _verify_artifacts(
        self,
        artifacts: Mapping[str, ArtifactRef],
        *,
        input_ref: bool,
    ) -> list[CacheMiss]:
        misses = []
        for name in sorted(artifacts):
            try:
                verification = self.artifact_store.verify(artifacts[name])
            except Exception as exc:
                reason = (
                    CacheMissReason.INPUT_VERIFICATION_FAILED
                    if input_ref
                    else CacheMissReason.OUTPUT_VERIFICATION_FAILED
                )
                misses.append(
                    CacheMiss(
                        reason,
                        subject=name,
                        detail=f"error_type={type(exc).__name__}",
                    )
                )
                continue
            if not verification.exists:
                reason = (
                    CacheMissReason.INPUT_NOT_FOUND
                    if input_ref
                    else CacheMissReason.OUTPUT_NOT_FOUND
                )
            elif not verification.size_matches:
                reason = (
                    CacheMissReason.INPUT_SIZE_MISMATCH
                    if input_ref
                    else CacheMissReason.OUTPUT_SIZE_MISMATCH
                )
            elif not verification.checksum_matches:
                reason = (
                    CacheMissReason.INPUT_CHECKSUM_MISMATCH
                    if input_ref
                    else CacheMissReason.OUTPUT_CHECKSUM_MISMATCH
                )
            else:
                continue
            misses.append(CacheMiss(reason, subject=name))
        return misses


def _input_fingerprint(
    inputs: Mapping[str, ArtifactRef],
) -> dict[str, object]:
    return {
        name: {
            "schema_version": artifact.schema_version,
            "kind": artifact.kind,
            "media_type": artifact.media_type,
            "size_bytes": artifact.size_bytes,
            "checksum": artifact.checksum.to_dict(),
        }
        for name, artifact in inputs.items()
    }
