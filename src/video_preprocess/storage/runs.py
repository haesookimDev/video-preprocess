"""Run Store port for versioned run and Stage manifests."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from video_preprocess.domain import (
    RunManifest,
    StageAttemptRef,
    StageManifest,
)


class RunStore(Protocol):
    """Persists run state separately from artifact bodies."""

    def save_run(self, manifest: RunManifest) -> None: ...

    def load_run(self, run_id: str) -> RunManifest | None: ...

    def save_stage(self, manifest: StageManifest) -> None: ...

    def load_stage(
        self,
        run_id: str,
        stage: StageAttemptRef,
    ) -> StageManifest | None: ...

    def is_stage_complete(
        self,
        run_id: str,
        stage: StageAttemptRef,
    ) -> bool: ...

    def find_stages_by_cache_key(
        self,
        cache_key: str,
    ) -> Sequence[StageManifest]: ...
