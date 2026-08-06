"""Tests for Executor handle, state, and Stage binding contracts."""

import pytest

from video_preprocess.executors import (
    DuplicateStageBindingError,
    ExecutionHandle,
    ExecutionState,
    ExecutionStatus,
    StageBindingRegistry,
    UnknownStageBindingError,
)


def runner(task):
    return None


def test_execution_handle_and_status_validation() -> None:
    handle = ExecutionHandle(
        execution_id="exec_123",
        stage_run_id="stage_123",
        attempt=1,
    )
    status = ExecutionStatus(handle, ExecutionState.QUEUED)

    assert status.handle == handle
    assert not status.state.terminal
    assert ExecutionState.SUCCEEDED.terminal

    with pytest.raises(ValueError, match="execution_id"):
        ExecutionHandle("", "stage_123", 1)
    with pytest.raises(ValueError, match="attempt"):
        ExecutionHandle("exec_123", "stage_123", 0)
    with pytest.raises(TypeError, match="ExecutionState"):
        ExecutionStatus(handle, "queued")


def test_binding_registry_indexes_and_sorts_stage_names() -> None:
    registry = StageBindingRegistry(
        [("02_second", runner), ("01_first", runner)]
    )

    assert registry.names == ("01_first", "02_second")
    assert registry.get("01_first") is runner


def test_binding_registry_rejects_duplicate_or_invalid_runner() -> None:
    with pytest.raises(DuplicateStageBindingError, match="duplicate"):
        StageBindingRegistry([("stage", runner), ("stage", runner)])
    with pytest.raises(TypeError, match="callable"):
        StageBindingRegistry([("stage", object())])


def test_binding_registry_reports_unknown_stage() -> None:
    registry = StageBindingRegistry([])

    with pytest.raises(UnknownStageBindingError, match="missing"):
        registry.get("missing")
