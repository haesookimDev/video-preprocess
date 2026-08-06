"""Pipeline registry and deterministic DAG planning."""

from .defaults import DEFAULT_STAGE_SPECS, create_default_registry
from .errors import (
    DependencyCycleError,
    DuplicateOutputError,
    DuplicateStageError,
    EngineConfigurationError,
    EngineInputError,
    InvalidInputDependencyError,
    PlanSelectionError,
    StateTransitionError,
    UnknownDependencyError,
)
from .planner import DAGPlanner, ExecutionPlan
from .pipeline import (
    PipelineEngine,
    PipelineRunResult,
    RunStateMachine,
    StageExecutionRecord,
    StageLifecycle,
    StageStateMachine,
)
from .registry import StageRegistry

__all__ = [
    "DAGPlanner",
    "DEFAULT_STAGE_SPECS",
    "DependencyCycleError",
    "DuplicateOutputError",
    "DuplicateStageError",
    "EngineConfigurationError",
    "EngineInputError",
    "ExecutionPlan",
    "InvalidInputDependencyError",
    "PipelineEngine",
    "PipelineRunResult",
    "PlanSelectionError",
    "StageRegistry",
    "StageExecutionRecord",
    "StageLifecycle",
    "StageStateMachine",
    "StateTransitionError",
    "UnknownDependencyError",
    "create_default_registry",
    "RunStateMachine",
]
