"""Task-specific adapter for normalized text embedding responses."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from video_preprocess.domain import (
    EffectiveModel,
    InferenceErrorCode,
    InferenceFailure,
    InferenceRequest,
    InferenceStatus,
    InferenceTask,
    RequestedModel,
)

from .errors import InferenceCallError
from .gateway import InferenceGateway


@dataclass(frozen=True, slots=True)
class EmbeddingBatch:
    """Validated embedding matrix returned to index and query adapters."""

    vectors: tuple[tuple[float, ...], ...]
    dimension: int
    model: EffectiveModel
    usage: dict[str, object]
    timing: dict[str, object]


class EmbeddingService:
    """Builds text-embedding requests and validates provider outputs."""

    def __init__(
        self,
        gateway: InferenceGateway,
        *,
        alias: str,
        model_name: str,
        revision: str,
        timeout_sec: float = 300.0,
    ) -> None:
        self.gateway = gateway
        self.alias = alias
        self.model_name = model_name
        self.revision = revision
        self.timeout_sec = timeout_sec

    def embed(
        self,
        texts: Sequence[str],
        *,
        run_id: str = "compat_query",
        stage_run_id: str = "text_embedding",
        trace_id: str | None = None,
    ) -> EmbeddingBatch:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.embed_async(
                    texts,
                    run_id=run_id,
                    stage_run_id=stage_run_id,
                    trace_id=trace_id,
                )
            )
        raise RuntimeError(
            "EmbeddingService.embed() cannot run inside an event loop; "
            "use await embed_async()"
        )

    async def embed_async(
        self,
        texts: Sequence[str],
        *,
        run_id: str = "compat_query",
        stage_run_id: str = "text_embedding",
        trace_id: str | None = None,
    ) -> EmbeddingBatch:
        normalized_texts = self._normalize_texts(texts)
        request_id = f"infer_{uuid.uuid4().hex}"
        trace_id = trace_id or f"trace_{uuid.uuid4().hex}"
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "alias": self.alias,
                    "model": self.model_name,
                    "revision": self.revision,
                    "texts": normalized_texts,
                    "normalize_embeddings": True,
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        request = InferenceRequest(
            request_id=request_id,
            idempotency_key=f"embed_{fingerprint}",
            run_id=run_id,
            stage_run_id=stage_run_id,
            task=InferenceTask.TEXT_EMBEDDING,
            model=RequestedModel(
                alias=self.alias,
                name=self.model_name,
                revision=self.revision,
            ),
            inputs={"texts": normalized_texts},
            parameters={"normalize_embeddings": True},
            timeout_sec=self.timeout_sec,
            trace_id=trace_id,
        )
        response = await self.gateway.infer(request)
        if response.status is not InferenceStatus.SUCCEEDED:
            failure = response.error or InferenceFailure(
                code=InferenceErrorCode.INFERENCE_FAILED,
                message="provider failed without an error object",
                retryable=False,
                request_id=request_id,
            )
            raise InferenceCallError(failure)
        if response.model is None:
            raise InferenceCallError(
                InferenceFailure(
                    code=InferenceErrorCode.INFERENCE_FAILED,
                    message="successful embedding response has no model",
                    retryable=False,
                    request_id=request_id,
                )
            )
        vectors = self._normalize_vectors(
            response.outputs.get("vectors"),
            expected_count=len(normalized_texts),
            request_id=request_id,
        )
        declared_dimension = response.outputs.get("dimension")
        if declared_dimension is not None and declared_dimension != len(
            vectors[0]
        ):
            raise InferenceCallError(
                InferenceFailure(
                    code=InferenceErrorCode.INFERENCE_FAILED,
                    message="embedding response dimension does not match vectors",
                    retryable=False,
                    request_id=request_id,
                )
            )
        return EmbeddingBatch(
            vectors=vectors,
            dimension=len(vectors[0]),
            model=response.model,
            usage=dict(response.usage),
            timing=dict(response.timing),
        )

    @staticmethod
    def _normalize_texts(texts: Sequence[str]) -> list[str]:
        if isinstance(texts, (str, bytes)) or not isinstance(texts, Sequence):
            raise ValueError("texts must be a sequence of strings")
        normalized = list(texts)
        if not normalized:
            raise ValueError("texts must not be empty")
        for index, text in enumerate(normalized):
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"texts[{index}] must be a non-empty string")
        return normalized

    @staticmethod
    def _normalize_vectors(
        value: object,
        *,
        expected_count: int,
        request_id: str,
    ) -> tuple[tuple[float, ...], ...]:
        if not isinstance(value, list) or len(value) != expected_count:
            raise InferenceCallError(
                InferenceFailure(
                    code=InferenceErrorCode.INFERENCE_FAILED,
                    message="embedding response has an invalid vector count",
                    retryable=False,
                    request_id=request_id,
                )
            )
        vectors = []
        dimension = None
        for row_index, row in enumerate(value):
            if not isinstance(row, list) or not row:
                raise InferenceCallError(
                    InferenceFailure(
                        code=InferenceErrorCode.INFERENCE_FAILED,
                        message=f"vector {row_index} is not a non-empty array",
                        retryable=False,
                        request_id=request_id,
                    )
                )
            try:
                vector = tuple(float(component) for component in row)
            except (TypeError, ValueError) as exc:
                raise InferenceCallError(
                    InferenceFailure(
                        code=InferenceErrorCode.INFERENCE_FAILED,
                        message=f"vector {row_index} contains non-numeric values",
                        retryable=False,
                        request_id=request_id,
                    )
                ) from exc
            if not all(math.isfinite(component) for component in vector):
                raise InferenceCallError(
                    InferenceFailure(
                        code=InferenceErrorCode.INFERENCE_FAILED,
                        message=f"vector {row_index} contains non-finite values",
                        retryable=False,
                        request_id=request_id,
                    )
                )
            if dimension is None:
                dimension = len(vector)
            elif len(vector) != dimension:
                raise InferenceCallError(
                    InferenceFailure(
                        code=InferenceErrorCode.INFERENCE_FAILED,
                        message="embedding vectors have inconsistent dimensions",
                        retryable=False,
                        request_id=request_id,
                    )
                )
            vectors.append(vector)
        return tuple(vectors)
