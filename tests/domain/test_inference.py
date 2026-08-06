"""Tests for versioned inference request, response, and capability contracts."""

import json

import pytest

from video_preprocess.domain import (
    ArtifactRef,
    Checksum,
    ContractValidationError,
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


def make_request() -> InferenceRequest:
    artifact = ArtifactRef(
        artifact_id="art_audio",
        kind="audio",
        uri="artifact://run_123/04_audio/audio.wav",
        media_type="audio/wav",
        size_bytes=128,
        checksum=Checksum("sha256", "abc123"),
    )
    return InferenceRequest(
        request_id="infer_123",
        idempotency_key="idem_123",
        run_id="run_123",
        stage_run_id="stage_456",
        task=InferenceTask.TEXT_EMBEDDING,
        model=RequestedModel(
            alias="embedding.default",
            name="example/model",
            revision="main",
        ),
        inputs={"texts": ["첫 번째", "두 번째"], "source": artifact},
        parameters={"normalize_embeddings": True},
        timeout_sec=30,
        trace_id="trace_123",
    )


def test_inference_request_round_trip_supports_inline_and_artifact_inputs() -> None:
    request = make_request()

    restored = InferenceRequest.from_dict(
        json.loads(json.dumps(request.to_dict(), ensure_ascii=False))
    )

    assert restored == request
    assert restored.task is InferenceTask.TEXT_EMBEDDING
    assert isinstance(restored.inputs["source"], ArtifactRef)
    assert restored.inputs["texts"] == ["첫 번째", "두 번째"]


def test_success_response_round_trip_records_effective_model() -> None:
    response = InferenceResponse(
        request_id="infer_123",
        status=InferenceStatus.SUCCEEDED,
        outputs={"vectors": [[1.0, 0.0], [0.0, 1.0]]},
        model=EffectiveModel(
            provider="local.embedding",
            name="example/model",
            revision="main",
            runtime="sentence-transformers/5.0",
        ),
        usage={"input_count": 2},
        timing={"inference_sec": 0.01},
    )

    restored = InferenceResponse.from_dict(
        json.loads(json.dumps(response.to_dict(), allow_nan=False))
    )

    assert restored == response
    assert restored.model == response.model


def test_failed_response_requires_structured_error() -> None:
    with pytest.raises(ContractValidationError) as exc_info:
        InferenceResponse(
            request_id="infer_123",
            status=InferenceStatus.FAILED,
        )

    assert exc_info.value.field == "error"


def test_cancelled_response_requires_cancelled_code() -> None:
    with pytest.raises(ContractValidationError) as exc_info:
        InferenceResponse(
            request_id="infer_123",
            status=InferenceStatus.CANCELLED,
            error=InferenceFailure(
                code=InferenceErrorCode.INFERENCE_FAILED,
                message="wrong code",
                retryable=False,
                request_id="infer_123",
            ),
        )

    assert exc_info.value.field == "error.code"


def test_capabilities_and_health_round_trip() -> None:
    capabilities = ProviderCapabilities(
        provider="local.embedding",
        tasks=[InferenceTask.TEXT_EMBEDDING],
        model_aliases=["embedding.default"],
        input_media_types=["text/plain"],
        features=["normalized_vectors"],
        max_batch_size=128,
    )
    health = ProviderHealth(
        provider="local.embedding",
        status=HealthState.AVAILABLE,
        details={"model_loaded": False},
    )

    assert ProviderCapabilities.from_dict(capabilities.to_dict()) == capabilities
    assert ProviderHealth.from_dict(health.to_dict()) == health

