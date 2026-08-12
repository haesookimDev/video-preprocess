"""Structural checks for the HTTP Inference OpenAPI v1 contract."""

from pathlib import Path

import yaml

from video_preprocess.domain import (
    InferenceFailure,
    InferenceRequest,
    InferenceResponse,
    ProviderCapabilities,
    ProviderHealth,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = PROJECT_ROOT / "docs" / "openapi" / "inference-v1.yaml"


def load_spec() -> dict[str, object]:
    data = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_openapi_declares_versioned_async_job_surface() -> None:
    spec = load_spec()

    assert spec["openapi"] == "3.1.0"
    paths = spec["paths"]
    assert set(paths) == {
        "/v1/health",
        "/v1/capabilities",
        "/v1/inference-jobs",
        "/v1/inference-jobs/{request_id}",
    }
    assert set(paths["/v1/inference-jobs"]) == {"post"}
    assert {
        "parameters",
        "get",
        "delete",
    } == set(paths["/v1/inference-jobs/{request_id}"])


def test_submit_requires_matching_idempotency_transport_fields() -> None:
    spec = load_spec()
    components = spec["components"]
    parameter = components["parameters"]["IdempotencyKey"]
    request_schema = components["schemas"]["InferenceRequest"]

    assert parameter["name"] == "Idempotency-Key"
    assert parameter["in"] == "header"
    assert parameter["required"] is True
    assert "idempotency_key" in request_schema["required"]
    assert "match" in parameter["description"].lower()


def test_openapi_domain_examples_round_trip_through_python_contracts() -> None:
    schemas = load_spec()["components"]["schemas"]

    request = InferenceRequest.from_dict(schemas["InferenceRequest"]["example"])
    response = InferenceResponse.from_dict(
        schemas["InferenceResponse"]["example"]
    )
    failure = InferenceFailure.from_dict(
        schemas["InferenceFailure"]["example"]
    )
    capabilities = ProviderCapabilities.from_dict(
        schemas["ProviderCapabilities"]["example"]
    )
    health = ProviderHealth.from_dict(schemas["ProviderHealth"]["example"])

    assert request.to_dict() == schemas["InferenceRequest"]["example"]
    assert response.to_dict() == schemas["InferenceResponse"]["example"]
    assert failure.to_dict() == schemas["InferenceFailure"]["example"]
    assert capabilities.to_dict() == schemas["ProviderCapabilities"]["example"]
    assert health.to_dict() == schemas["ProviderHealth"]["example"]


def test_all_local_component_references_resolve() -> None:
    spec = load_spec()
    unresolved = []

    def visit(value: object) -> None:
        if isinstance(value, dict):
            reference = value.get("$ref")
            if isinstance(reference, str) and reference.startswith("#/"):
                target: object = spec
                for segment in reference[2:].split("/"):
                    if not isinstance(target, dict) or segment not in target:
                        unresolved.append(reference)
                        break
                    target = target[segment]
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(spec)

    assert unresolved == []


def test_contract_forbids_host_paths_and_declares_terminal_model_data() -> None:
    spec = load_spec()
    schemas = spec["components"]["schemas"]
    artifact_uri = schemas["ArtifactRef"]["properties"]["uri"]
    success = schemas["InferenceResponse"]["example"]

    assert artifact_uri["pattern"] == "^artifact://"
    assert "file" in artifact_uri["description"]
    assert success["model"]["provider"] == "http.embedding"
    assert success["model"]["revision"] == "commit-abc123"
    serialized = SPEC_PATH.read_text(encoding="utf-8")
    assert "Authorization:" not in serialized
    assert "/Users/" not in serialized
