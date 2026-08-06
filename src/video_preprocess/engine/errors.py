"""Configuration and selection errors raised by the pipeline engine."""


class EngineConfigurationError(ValueError):
    """Registered Stage metadata cannot form a valid pipeline."""


class DuplicateStageError(EngineConfigurationError):
    """More than one StageSpec uses the same stable name."""


class DuplicateOutputError(EngineConfigurationError):
    """More than one StageSpec publishes the same logical output key."""


class UnknownDependencyError(EngineConfigurationError):
    """A StageSpec refers to a stage that is not registered."""


class InvalidInputDependencyError(EngineConfigurationError):
    """A required input has no producer in the stage's ancestor graph."""


class DependencyCycleError(EngineConfigurationError):
    """Registered stage dependencies contain a cycle."""


class PlanSelectionError(ValueError):
    """A requested stage selection cannot produce an execution plan."""


class EngineInputError(ValueError):
    """A plan, config, binding, or boundary artifact is incomplete."""


class StateTransitionError(RuntimeError):
    """A run or Stage lifecycle attempted an invalid transition."""


class EnginePersistenceError(RuntimeError):
    """The Engine could not durably read or write run state."""
