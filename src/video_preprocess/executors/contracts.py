"""Executor Port and transport-neutral local execution identifiers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from video_preprocess.domain import StageResult, StageTask


def _required_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


class ExecutionState(str, Enum):
    """Executor lifecycle state for one submitted StageTask attempt."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {
            self.SUCCEEDED,
            self.SKIPPED,
            self.FAILED,
            self.CANCELLED,
        }


@dataclass(frozen=True, slots=True)
class ExecutionHandle:
    """Opaque lookup identity returned immediately after submission."""

    execution_id: str
    stage_run_id: str
    attempt: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "execution_id",
            _required_string(self.execution_id, "execution_id"),
        )
        object.__setattr__(
            self,
            "stage_run_id",
            _required_string(self.stage_run_id, "stage_run_id"),
        )
        if (
            isinstance(self.attempt, bool)
            or not isinstance(self.attempt, int)
            or self.attempt < 1
        ):
            raise ValueError("attempt must be a positive integer")


@dataclass(frozen=True, slots=True)
class ExecutionStatus:
    """Current Executor-owned state for one handle."""

    handle: ExecutionHandle
    state: ExecutionState
    cancel_requested: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.handle, ExecutionHandle):
            raise TypeError("handle must be an ExecutionHandle")
        if not isinstance(self.state, ExecutionState):
            raise TypeError("state must be an ExecutionState")
        if not isinstance(self.cancel_requested, bool):
            raise TypeError("cancel_requested must be a boolean")


class Executor(Protocol):
    """Where and how StageTask attempts execute."""

    async def submit(self, task: StageTask) -> ExecutionHandle: ...

    async def status(self, handle: ExecutionHandle) -> ExecutionStatus: ...

    async def result(self, handle: ExecutionHandle) -> StageResult: ...

    async def cancel(self, handle: ExecutionHandle) -> None: ...
