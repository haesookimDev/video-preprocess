"""Engine-owned bounded retry policy for Stage attempts."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from video_preprocess.domain import StageResult, StageStatus


DEFAULT_RETRYABLE_REASONS = (
    "EXECUTOR_RESULT_FAILED",
    "EXECUTOR_SUBMIT_FAILED",
    "STAGE_TIMEOUT",
)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Retry only classified transient Stage failures within a hard limit."""

    max_attempts: int = 1
    initial_backoff_sec: float = 0.0
    backoff_multiplier: float = 2.0
    max_backoff_sec: float = 30.0
    retryable_reason_codes: Collection[str] = DEFAULT_RETRYABLE_REASONS

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or self.max_attempts < 1
        ):
            raise ValueError("max_attempts must be a positive integer")
        for field_name, value, minimum in (
            ("initial_backoff_sec", self.initial_backoff_sec, 0.0),
            ("backoff_multiplier", self.backoff_multiplier, 1.0),
            ("max_backoff_sec", self.max_backoff_sec, 0.0),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or value < minimum
            ):
                raise ValueError(f"{field_name} must be at least {minimum}")
            object.__setattr__(self, field_name, float(value))
        if self.max_backoff_sec < self.initial_backoff_sec:
            raise ValueError(
                "max_backoff_sec must be at least initial_backoff_sec"
            )
        if isinstance(self.retryable_reason_codes, (str, bytes)):
            raise TypeError("retryable_reason_codes must be a collection")
        normalized = []
        for reason in self.retryable_reason_codes:
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError(
                    "retryable_reason_codes must contain non-empty strings"
                )
            normalized.append(reason.strip())
        object.__setattr__(
            self,
            "retryable_reason_codes",
            frozenset(normalized),
        )

    def should_retry(
        self,
        result: StageResult,
        *,
        attempts_used: int,
    ) -> bool:
        if not isinstance(result, StageResult):
            raise TypeError("result must be a StageResult")
        if (
            isinstance(attempts_used, bool)
            or not isinstance(attempts_used, int)
            or attempts_used < 1
        ):
            raise ValueError("attempts_used must be a positive integer")
        return (
            attempts_used < self.max_attempts
            and result.status is StageStatus.FAILED
            and result.reason_code in self.retryable_reason_codes
        )

    def backoff_sec(self, *, attempts_used: int) -> float:
        if (
            isinstance(attempts_used, bool)
            or not isinstance(attempts_used, int)
            or attempts_used < 1
        ):
            raise ValueError("attempts_used must be a positive integer")
        delay = self.initial_backoff_sec * (
            self.backoff_multiplier ** (attempts_used - 1)
        )
        return min(delay, self.max_backoff_sec)
