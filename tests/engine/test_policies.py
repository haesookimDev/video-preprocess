"""Tests for bounded Engine retry policy classification."""

import pytest

from video_preprocess.domain import StageResult, StageStatus
from video_preprocess.engine import RetryPolicy


def result(reason_code: str) -> StageResult:
    return StageResult(
        run_id="run-123",
        stage_run_id="stage-123",
        attempt=1,
        status=StageStatus.FAILED,
        reason_code=reason_code,
        reason="test failure",
    )


def test_retry_policy_bounds_classified_attempts_and_backoff() -> None:
    policy = RetryPolicy(
        max_attempts=4,
        initial_backoff_sec=0.5,
        backoff_multiplier=3,
        max_backoff_sec=2,
    )

    assert policy.should_retry(result("STAGE_TIMEOUT"), attempts_used=1)
    assert not policy.should_retry(
        result("INVALID_REQUEST"),
        attempts_used=1,
    )
    assert not policy.should_retry(
        result("STAGE_TIMEOUT"),
        attempts_used=4,
    )
    assert policy.backoff_sec(attempts_used=1) == 0.5
    assert policy.backoff_sec(attempts_used=2) == 1.5
    assert policy.backoff_sec(attempts_used=3) == 2.0


@pytest.mark.parametrize(
    "options",
    [
        {"max_attempts": 0},
        {"initial_backoff_sec": -1},
        {"backoff_multiplier": 0.5},
        {"initial_backoff_sec": 2, "max_backoff_sec": 1},
        {"retryable_reason_codes": [""]},
    ],
)
def test_retry_policy_rejects_invalid_configuration(options) -> None:
    with pytest.raises((TypeError, ValueError)):
        RetryPolicy(**options)
