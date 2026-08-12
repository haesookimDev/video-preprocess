"""Lazy, reusable SentenceTransformer embedding provider."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from typing import Protocol

from video_preprocess.domain import (
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
)
from video_preprocess.inference.embedding import EmbeddingService
from video_preprocess.inference.gateway import InferenceGateway

from .fingerprints import (
    resolve_hf_cache_revision,
    sentence_transformer_repo_id,
)


class EmbeddingModel(Protocol):
    """Subset of SentenceTransformer used by this provider."""

    def encode(
        self,
        sentences: Sequence[str],
        *,
        normalize_embeddings: bool,
    ) -> object: ...


ModelLoader = Callable[[str, str | None, str | None], EmbeddingModel]


def _default_loader(
    model_name: str,
    revision: str | None,
    device: str | None,
) -> EmbeddingModel:
    from sentence_transformers import SentenceTransformer

    options = {}
    if revision is not None:
        options["revision"] = revision
    if device is not None:
        options["device"] = device
    return SentenceTransformer(model_name, **options)


def _runtime_name() -> str:
    try:
        package_version = version("sentence-transformers")
    except PackageNotFoundError:
        package_version = "unknown"
    return f"sentence-transformers/{package_version}"


class LocalEmbeddingProvider:
    """In-process embedding provider with lazy model and response caches."""

    PROVIDER_NAME = "local.embedding"

    def __init__(
        self,
        *,
        alias: str,
        model_name: str,
        revision: str | None = None,
        device: str | None = None,
        max_batch_size: int = 128,
        loader: ModelLoader = _default_loader,
    ) -> None:
        if (
            not isinstance(alias, str)
            or not alias.strip()
            or not isinstance(model_name, str)
            or not model_name.strip()
        ):
            raise ValueError("alias and model_name must be non-empty")
        if (
            isinstance(max_batch_size, bool)
            or not isinstance(max_batch_size, int)
            or max_batch_size < 1
        ):
            raise ValueError("max_batch_size must be at least 1")
        if revision is not None and (
            not isinstance(revision, str) or not revision.strip()
        ):
            raise ValueError("revision must be a non-empty string or None")
        if device is not None and (
            not isinstance(device, str) or not device.strip()
        ):
            raise ValueError("device must be a non-empty string or None")
        self.alias = alias
        self.model_name = model_name
        self.revision = revision
        self.requested_revision = revision or "default"
        self.effective_revision = self.requested_revision
        self.device = device
        self.max_batch_size = max_batch_size
        self._loader = loader
        self._model: EmbeddingModel | None = None
        self._model_lock = threading.Lock()
        self._inference_lock = threading.Lock()
        self._load_error: str | None = None
        self._responses: dict[str, tuple[str, InferenceResponse]] = {}

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    async def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.PROVIDER_NAME,
            tasks=[InferenceTask.TEXT_EMBEDDING],
            model_aliases=[self.alias],
            input_media_types=["text/plain"],
            features=["normalized_vectors", "inline_batch"],
            max_batch_size=self.max_batch_size,
            supports_cancellation=False,
            supports_async_jobs=False,
        )

    async def health(self) -> ProviderHealth:
        if self._load_error is not None:
            return ProviderHealth(
                provider=self.PROVIDER_NAME,
                status=HealthState.UNAVAILABLE,
                details={"load_error": self._load_error},
            )
        return ProviderHealth(
            provider=self.PROVIDER_NAME,
            status=HealthState.AVAILABLE,
            details={"model_loaded": self.is_loaded},
        )

    async def effective_model(self) -> EffectiveModel | None:
        """Resolve the model this provider would use without loading it."""

        revision = self.effective_revision if self.is_loaded else await (
            asyncio.to_thread(
                resolve_hf_cache_revision,
                sentence_transformer_repo_id(self.model_name),
                "modules.json",
                self.revision,
            )
        )
        if revision is None:
            return None
        return EffectiveModel(
            provider=self.PROVIDER_NAME,
            name=self.model_name,
            revision=revision,
            runtime=_runtime_name(),
        )

    async def warmup(self) -> None:
        await asyncio.to_thread(self._get_model)

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        return await asyncio.to_thread(self._infer_sync, request)

    async def cancel(self, request_id: str) -> None:
        return None

    def _infer_sync(self, request: InferenceRequest) -> InferenceResponse:
        with self._inference_lock:
            fingerprint = self._fingerprint(request)
            cached = self._responses.get(request.idempotency_key)
            if cached is not None:
                cached_fingerprint, cached_response = cached
                if cached_fingerprint != fingerprint:
                    return self._failure(
                        request,
                        InferenceErrorCode.INVALID_REQUEST,
                        "idempotency key was reused with different input",
                        details={"reason": "IDEMPOTENCY_KEY_CONFLICT"},
                    )
                return self._with_request_id(
                    cached_response, request.request_id
                )

            validation_error = self._validate_request(request)
            if validation_error is not None:
                return validation_error

            texts = request.inputs["texts"]
            normalize_embeddings = request.parameters.get(
                "normalize_embeddings", True
            )
            try:
                model, load_elapsed = self._get_model()
            except Exception as exc:
                self._load_error = type(exc).__name__
                return self._failure(
                    request,
                    InferenceErrorCode.MODEL_UNAVAILABLE,
                    "embedding model could not be loaded",
                    details={"error_type": type(exc).__name__},
                )

            inference_start = time.monotonic()
            try:
                raw_vectors = model.encode(
                    texts,
                    normalize_embeddings=normalize_embeddings,
                )
                vectors = self._normalize_vectors(
                    raw_vectors,
                    len(texts),
                    normalize=normalize_embeddings,
                )
            except Exception as exc:
                return self._failure(
                    request,
                    InferenceErrorCode.INFERENCE_FAILED,
                    "embedding model execution failed",
                    details={"error_type": type(exc).__name__},
                )
            inference_elapsed = time.monotonic() - inference_start
            response = InferenceResponse(
                request_id=request.request_id,
                status=InferenceStatus.SUCCEEDED,
                outputs={
                    "vectors": vectors,
                    "dimension": len(vectors[0]),
                },
                model=EffectiveModel(
                    provider=self.PROVIDER_NAME,
                    name=self.model_name,
                    revision=self.effective_revision,
                    runtime=_runtime_name(),
                ),
                usage={
                    "input_count": len(texts),
                    "batch_size": len(texts),
                },
                timing={
                    "model_load_sec": round(load_elapsed, 6),
                    "inference_sec": round(inference_elapsed, 6),
                },
            )
            self._cache_response(request, fingerprint, response)
            return response

    def _get_model(self) -> tuple[EmbeddingModel, float]:
        if self._model is not None:
            return self._model, 0.0
        with self._model_lock:
            if self._model is not None:
                return self._model, 0.0
            started = time.monotonic()
            model = self._loader(
                self.model_name,
                self.revision,
                self.device,
            )
            self._model = model
            self.effective_revision = self._resolve_revision(
                model,
                fallback=self.requested_revision,
            )
            self._load_error = None
            return model, time.monotonic() - started

    def _validate_request(
        self,
        request: InferenceRequest,
    ) -> InferenceResponse | None:
        if request.task is not InferenceTask.TEXT_EMBEDDING:
            return self._failure(
                request,
                InferenceErrorCode.UNSUPPORTED_CAPABILITY,
                "local embedding provider only supports text_embedding",
            )
        if (
            request.model.alias != self.alias
            or request.model.name != self.model_name
            or request.model.revision != self.requested_revision
        ):
            return self._failure(
                request,
                InferenceErrorCode.UNSUPPORTED_CAPABILITY,
                "requested model does not match provider binding",
            )
        texts = request.inputs.get("texts")
        if (
            isinstance(texts, (str, bytes))
            or not isinstance(texts, list)
            or not texts
        ):
            return self._failure(
                request,
                InferenceErrorCode.INVALID_REQUEST,
                "inputs.texts must be a non-empty string array",
            )
        if len(texts) > self.max_batch_size:
            return self._failure(
                request,
                InferenceErrorCode.INVALID_REQUEST,
                "embedding batch exceeds provider maximum",
                details={"max_batch_size": self.max_batch_size},
            )
        if any(not isinstance(text, str) or not text.strip() for text in texts):
            return self._failure(
                request,
                InferenceErrorCode.INVALID_REQUEST,
                "inputs.texts contains an empty or non-string value",
            )
        normalize_embeddings = request.parameters.get(
            "normalize_embeddings", True
        )
        if not isinstance(normalize_embeddings, bool):
            return self._failure(
                request,
                InferenceErrorCode.INVALID_REQUEST,
                "parameters.normalize_embeddings must be a boolean",
            )
        return None

    @staticmethod
    def _resolve_revision(model: EmbeddingModel, *, fallback: str) -> str:
        first_module_getter = getattr(model, "_first_module", None)
        if not callable(first_module_getter):
            return fallback
        try:
            first_module = first_module_getter()
            auto_model = getattr(first_module, "auto_model", None)
            config = getattr(auto_model, "config", None)
            commit_hash = getattr(config, "_commit_hash", None)
        except Exception:
            return fallback
        if isinstance(commit_hash, str) and commit_hash.strip():
            return commit_hash
        return fallback

    @staticmethod
    def _normalize_vectors(
        value: object,
        expected_count: int,
        *,
        normalize: bool,
    ) -> list[list[float]]:
        if hasattr(value, "tolist"):
            value = value.tolist()
        if not isinstance(value, list) or len(value) != expected_count:
            raise ValueError("embedding output count does not match input")
        vectors = []
        dimension = None
        for row in value:
            if hasattr(row, "tolist"):
                row = row.tolist()
            if not isinstance(row, list) or not row:
                raise ValueError("embedding output row must be non-empty")
            vector = [float(component) for component in row]
            if not all(math.isfinite(component) for component in vector):
                raise ValueError("embedding output contains non-finite values")
            if normalize:
                norm = math.sqrt(sum(component * component for component in vector))
                if norm == 0:
                    raise ValueError("embedding output contains a zero vector")
                vector = [component / norm for component in vector]
            if dimension is None:
                dimension = len(vector)
            elif len(vector) != dimension:
                raise ValueError("embedding output dimensions are inconsistent")
            vectors.append(vector)
        return vectors

    @staticmethod
    def _fingerprint(request: InferenceRequest) -> str:
        payload = {
            "task": request.task.value,
            "model": request.model.to_dict(),
            "inputs": request.to_dict()["inputs"],
            "parameters": dict(request.parameters),
        }
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def _cache_response(
        self,
        request: InferenceRequest,
        fingerprint: str,
        response: InferenceResponse,
    ) -> None:
        if len(self._responses) >= 256:
            self._responses.pop(next(iter(self._responses)))
        self._responses[request.idempotency_key] = (fingerprint, response)

    @staticmethod
    def _with_request_id(
        response: InferenceResponse,
        request_id: str,
    ) -> InferenceResponse:
        error = response.error
        if error is not None:
            error = replace(error, request_id=request_id)
        return replace(response, request_id=request_id, error=error)

    @staticmethod
    def _failure(
        request: InferenceRequest,
        code: InferenceErrorCode,
        message: str,
        *,
        details: dict[str, object] | None = None,
    ) -> InferenceResponse:
        return InferenceResponse(
            request_id=request.request_id,
            status=InferenceStatus.FAILED,
            error=InferenceFailure(
                code=code,
                message=message,
                retryable=False,
                details={} if details is None else details,
                request_id=request.request_id,
            ),
        )


@lru_cache(maxsize=8)
def get_local_embedding_service(
    model_name: str,
    *,
    alias: str = "embedding.default",
    revision: str | None = None,
    device: str | None = None,
) -> EmbeddingService:
    """Reuse one provider/model instance per local process and binding."""

    provider = LocalEmbeddingProvider(
        alias=alias,
        model_name=model_name,
        revision=revision,
        device=device,
    )
    gateway = InferenceGateway({alias: provider})
    return EmbeddingService(
        gateway,
        alias=alias,
        model_name=model_name,
        revision=provider.requested_revision,
    )
