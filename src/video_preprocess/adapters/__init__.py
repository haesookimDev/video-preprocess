"""Outer compatibility and delivery adapters."""

from .legacy_stages import (
    LegacyStageContractError,
    LegacyStageOutcome,
    LegacyStageTaskRunner,
    create_legacy_media_bindings,
    create_legacy_model_bindings,
)

__all__ = [
    "LegacyStageContractError",
    "LegacyStageOutcome",
    "LegacyStageTaskRunner",
    "create_legacy_media_bindings",
    "create_legacy_model_bindings",
]
