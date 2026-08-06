"""Pipeline registry and deterministic DAG planning."""

from .defaults import DEFAULT_STAGE_SPECS, create_default_registry
from .errors import (
    DependencyCycleError,
    DuplicateOutputError,
    DuplicateStageError,
    EngineConfigurationError,
    InvalidInputDependencyError,
    PlanSelectionError,
    UnknownDependencyError,
)
from .planner import DAGPlanner, ExecutionPlan
from .registry import StageRegistry

__all__ = [
    "DAGPlanner",
    "DEFAULT_STAGE_SPECS",
    "DependencyCycleError",
    "DuplicateOutputError",
    "DuplicateStageError",
    "EngineConfigurationError",
    "ExecutionPlan",
    "InvalidInputDependencyError",
    "PlanSelectionError",
    "StageRegistry",
    "UnknownDependencyError",
    "create_default_registry",
]
