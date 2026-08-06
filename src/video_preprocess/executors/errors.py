"""Errors exposed by Executor bindings and handle lookup."""


class ExecutorError(RuntimeError):
    """Base error raised by Executor infrastructure."""


class DuplicateStageBindingError(ExecutorError):
    """More than one runner is bound to the same stable Stage name."""


class UnknownStageBindingError(ExecutorError):
    """No runner is bound for a submitted StageTask."""


class DuplicateSubmissionError(ExecutorError):
    """One logical stage attempt was submitted with different identity."""


class IdempotencyConflictError(ExecutorError):
    """An idempotency key was reused for a different StageTask."""


class UnknownExecutionError(ExecutorError):
    """An ExecutionHandle is not owned by this Executor instance."""
