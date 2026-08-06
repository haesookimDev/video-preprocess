"""Public domain contracts shared by engines, executors, and adapters."""

from .artifacts import ArtifactRef, Checksum
from .errors import ContractValidationError, UnsupportedSchemaVersion
from .inference import (
    EffectiveModel,
    HealthState,
    InferenceErrorCode,
    InferenceFailure,
    InferenceRequest,
    InferenceResponse,
    InferenceStatus,
    InferenceTask,
    ProviderCapabilities,
    ProviderHealth,
    RequestedModel,
)
from .manifests import (
    RunManifest,
    RunStatus,
    StageAttemptRef,
    StageManifest,
)
from .stages import (
    ModelExecution,
    ResourceHints,
    StageResult,
    StageSpec,
    StageStatus,
    StageTask,
)

__all__ = [
    "ArtifactRef",
    "Checksum",
    "ContractValidationError",
    "EffectiveModel",
    "HealthState",
    "InferenceErrorCode",
    "InferenceFailure",
    "InferenceRequest",
    "InferenceResponse",
    "InferenceStatus",
    "InferenceTask",
    "ModelExecution",
    "ProviderCapabilities",
    "ProviderHealth",
    "ResourceHints",
    "RequestedModel",
    "RunManifest",
    "RunStatus",
    "StageAttemptRef",
    "StageManifest",
    "StageResult",
    "StageSpec",
    "StageStatus",
    "StageTask",
    "UnsupportedSchemaVersion",
]
