"""Adapters from inference gateways to Engine model fingerprints."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from video_preprocess.domain import ModelExecution, StageTask

from .gateway import InferenceGateway


class GatewayEffectiveModelResolver:
    """Resolve each Stage slot through its currently bound gateway."""

    def __init__(self, gateways: Mapping[str, InferenceGateway]) -> None:
        normalized = {}
        for alias, gateway in gateways.items():
            if not isinstance(alias, str) or not alias.strip():
                raise ValueError("gateway alias must be a non-empty string")
            if not isinstance(gateway, InferenceGateway):
                raise TypeError("gateways must contain InferenceGateway values")
            normalized[alias] = gateway
        self.gateways = normalized

    async def resolve(
        self,
        task: StageTask,
    ) -> Sequence[ModelExecution] | None:
        if not isinstance(task, StageTask):
            raise TypeError("task must be a StageTask")
        resolved = []
        for slot, alias in sorted(task.model_bindings.items()):
            gateway = self.gateways.get(alias)
            if gateway is None:
                return None
            model = await gateway.effective_model(alias)
            if model is None:
                return None
            resolved.append(
                ModelExecution(
                    slot=slot,
                    provider=model.provider,
                    model=model.name,
                    revision=model.revision,
                    runtime=model.runtime,
                )
            )
        return tuple(resolved)
