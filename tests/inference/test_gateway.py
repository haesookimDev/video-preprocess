"""Contract tests for alias-based inference routing."""

import asyncio
from dataclasses import replace

from video_preprocess.domain import (
    ArtifactRef,
    Checksum,
    EffectiveModel,
    HealthState,
    InferenceErrorCode,
    InferenceRequest,
    InferenceResponse,
    InferenceStatus,
    InferenceTask,
    ProviderCapabilities,
    ProviderHealth,
    RequestedModel,
)
from video_preprocess.inference import InferenceGateway


def make_request(
    *,
    alias: str = "embedding.default",
    task: InferenceTask = InferenceTask.TEXT_EMBEDDING,
    timeout_sec: float = 1.0,
) -> InferenceRequest:
    return InferenceRequest(
        request_id="infer_123",
        idempotency_key="idem_123",
        run_id="run_123",
        stage_run_id="stage_456",
        task=task,
        model=RequestedModel(
            alias=alias,
            name="example/model",
            revision="main",
        ),
        inputs={"texts": ["테스트"]},
        parameters={},
        timeout_sec=timeout_sec,
        trace_id="trace_123",
    )


class FakeProvider:
    def __init__(self) -> None:
        self.infer_count = 0

    async def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider="fake",
            tasks=[InferenceTask.TEXT_EMBEDDING],
            model_aliases=["embedding.default"],
        )

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        self.infer_count += 1
        return InferenceResponse(
            request_id=request.request_id,
            status=InferenceStatus.SUCCEEDED,
            outputs={"vectors": [[1.0, 0.0]]},
            model=EffectiveModel(
                provider="fake",
                name=request.model.name,
                revision=request.model.revision,
            ),
        )

    async def cancel(self, request_id: str) -> None:
        return None

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider="fake",
            status=HealthState.AVAILABLE,
        )


def test_gateway_routes_bound_alias() -> None:
    provider = FakeProvider()
    gateway = InferenceGateway({"embedding.default": provider})

    response = asyncio.run(gateway.infer(make_request()))

    assert response.status is InferenceStatus.SUCCEEDED
    assert response.model is not None
    assert response.model.provider == "fake"
    assert provider.infer_count == 1


def test_gateway_rejects_unbound_alias_without_calling_provider() -> None:
    provider = FakeProvider()
    gateway = InferenceGateway({"embedding.default": provider})

    response = asyncio.run(
        gateway.infer(make_request(alias="embedding.missing"))
    )

    assert response.status is InferenceStatus.FAILED
    assert response.error is not None
    assert response.error.code is InferenceErrorCode.UNSUPPORTED_CAPABILITY
    assert provider.infer_count == 0


def test_gateway_rejects_unsupported_task_before_inference() -> None:
    provider = FakeProvider()
    gateway = InferenceGateway({"embedding.default": provider})

    response = asyncio.run(
        gateway.infer(make_request(task=InferenceTask.IMAGE_CAPTIONING))
    )

    assert response.error is not None
    assert response.error.code is InferenceErrorCode.UNSUPPORTED_CAPABILITY
    assert provider.infer_count == 0


def test_gateway_rejects_batch_larger_than_capability() -> None:
    provider = FakeProvider()
    gateway = InferenceGateway({"embedding.default": provider})
    request = replace(make_request(), inputs={"texts": ["하나", "둘"]})

    response = asyncio.run(gateway.infer(request))

    assert response.error is not None
    assert response.error.code is InferenceErrorCode.UNSUPPORTED_CAPABILITY
    assert response.error.details["max_batch_size"] == 1
    assert provider.infer_count == 0


def test_gateway_checks_artifact_image_batch_limit() -> None:
    class ImageProvider(FakeProvider):
        async def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(
                provider="fake.caption",
                tasks=[InferenceTask.IMAGE_CAPTIONING],
                model_aliases=["caption.default"],
                max_batch_size=1,
            )

    artifact = ArtifactRef(
        artifact_id="image_1",
        kind="image",
        uri="artifact://run_123/frames/1.jpg",
        media_type="image/jpeg",
        size_bytes=10,
        checksum=Checksum("sha256", "abc"),
    )
    request = make_request(
        alias="caption.default",
        task=InferenceTask.IMAGE_CAPTIONING,
    )
    request = replace(request, inputs={"images": [artifact, artifact]})
    provider = ImageProvider()

    response = asyncio.run(
        InferenceGateway({"caption.default": provider}).infer(request)
    )

    assert response.error is not None
    assert response.error.code is InferenceErrorCode.UNSUPPORTED_CAPABILITY
    assert response.error.details["max_batch_size"] == 1
    assert provider.infer_count == 0


def test_gateway_checks_nested_artifact_size_limit() -> None:
    class ImageProvider(FakeProvider):
        async def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(
                provider="fake.caption",
                tasks=[InferenceTask.IMAGE_CAPTIONING],
                model_aliases=["caption.default"],
                max_batch_size=2,
                max_artifact_bytes=5,
            )

    artifact = ArtifactRef(
        artifact_id="image_1",
        kind="image",
        uri="artifact://run_123/frames/1.jpg",
        media_type="image/jpeg",
        size_bytes=10,
        checksum=Checksum("sha256", "abc"),
    )
    request = make_request(
        alias="caption.default",
        task=InferenceTask.IMAGE_CAPTIONING,
    )
    request = replace(request, inputs={"images": [artifact]})
    provider = ImageProvider()

    response = asyncio.run(
        InferenceGateway({"caption.default": provider}).infer(request)
    )

    assert response.error is not None
    assert response.error.code is InferenceErrorCode.UNSUPPORTED_CAPABILITY
    assert "images[0]" in response.error.message
    assert provider.infer_count == 0


def test_gateway_normalizes_provider_exception() -> None:
    class BrokenProvider(FakeProvider):
        async def infer(self, request: InferenceRequest) -> InferenceResponse:
            raise ConnectionError("provider is down")

    gateway = InferenceGateway({"embedding.default": BrokenProvider()})

    response = asyncio.run(gateway.infer(make_request()))

    assert response.error is not None
    assert response.error.code is InferenceErrorCode.PROVIDER_UNAVAILABLE
    assert response.error.retryable
    assert response.error.details["error_type"] == "ConnectionError"


def test_gateway_enforces_total_timeout() -> None:
    class SlowProvider(FakeProvider):
        async def capabilities(self) -> ProviderCapabilities:
            await asyncio.sleep(0.02)
            return await super().capabilities()

    gateway = InferenceGateway({"embedding.default": SlowProvider()})

    response = asyncio.run(
        gateway.infer(make_request(timeout_sec=0.001))
    )

    assert response.error is not None
    assert response.error.code is InferenceErrorCode.PROVIDER_TIMEOUT
