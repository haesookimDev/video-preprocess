"""Executor Port and transport-neutral local execution identifiers."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from dataclasses import field
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


class CancellationToken:
    """Thread-safe cooperative cancellation signal for one Stage attempt."""

    def __init__(self) -> None:
        self._event = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    async def wait(self, *, poll_interval_sec: float = 0.01) -> None:
        if (
            isinstance(poll_interval_sec, bool)
            or not isinstance(poll_interval_sec, (int, float))
            or poll_interval_sec <= 0
        ):
            raise ValueError("poll_interval_sec must be positive")
        while not self.cancelled:
            await asyncio.sleep(float(poll_interval_sec))


@dataclass(frozen=True, slots=True)
class ExecutionControl:
    """Non-serialized deadline and cancellation context for an Executor."""

    timeout_sec: float | None = None
    cancellation: CancellationToken = field(default_factory=CancellationToken)

    def __post_init__(self) -> None:
        if self.timeout_sec is not None and (
            isinstance(self.timeout_sec, bool)
            or not isinstance(self.timeout_sec, (int, float))
            or self.timeout_sec <= 0
        ):
            raise ValueError("timeout_sec must be positive or None")
        if not isinstance(self.cancellation, CancellationToken):
            raise TypeError("cancellation must be a CancellationToken")
        if self.timeout_sec is not None:
            object.__setattr__(self, "timeout_sec", float(self.timeout_sec))


class Executor(Protocol):
    """Where and how StageTask attempts execute."""

    async def submit(
        self,
        task: StageTask,
        *,
        control: ExecutionControl | None = None,
    ) -> ExecutionHandle: ...

    async def status(self, handle: ExecutionHandle) -> ExecutionStatus: ...

    async def result(self, handle: ExecutionHandle) -> StageResult: ...

    async def cancel(self, handle: ExecutionHandle) -> None: ...
