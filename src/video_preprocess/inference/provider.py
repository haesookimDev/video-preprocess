"""Inference Provider port implemented by local and remote backends."""

from __future__ import annotations

from typing import Protocol

from video_preprocess.domain import (
    InferenceRequest,
    InferenceResponse,
    ProviderCapabilities,
    ProviderHealth,
)


class InferenceProvider(Protocol):
    """Asynchronous boundary around one inference backend."""

    async def capabilities(self) -> ProviderCapabilities: ...

    async def infer(self, request: InferenceRequest) -> InferenceResponse: ...

    async def cancel(self, request_id: str) -> None: ...

    async def health(self) -> ProviderHealth: ...

