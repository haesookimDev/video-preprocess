"""Runtime errors exposed by inference services and configuration."""

from video_preprocess.domain import InferenceFailure


class ProviderConfigurationError(ValueError):
    """Provider bindings are missing, duplicated, or inconsistent."""


class InferenceCallError(RuntimeError):
    """A normalized provider response reports inference failure."""

    def __init__(self, failure: InferenceFailure) -> None:
        self.failure = failure
        super().__init__(f"{failure.code.value}: {failure.message}")

