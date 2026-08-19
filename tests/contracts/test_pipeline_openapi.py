"""Structural checks for the public Pipeline REST API v1 contract."""

from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = PROJECT_ROOT / "docs" / "openapi" / "pipeline-v1.yaml"


def load_spec() -> dict[str, object]:
    data = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_openapi_declares_minimum_pipeline_and_query_surface() -> None:
    spec = load_spec()

    assert spec["openapi"] == "3.1.0"
    assert set(spec["paths"]) == {
        "/v1/pipeline-runs",
        "/v1/pipeline-runs/{run_id}",
        "/v1/pipeline-runs/{run_id}/artifacts",
        "/v1/pipeline-runs/{run_id}/queries",
    }
    assert set(spec["paths"]["/v1/pipeline-runs"]) == {"post"}
    assert set(spec["paths"]["/v1/pipeline-runs/{run_id}"]) == {
        "parameters",
        "get",
        "delete",
    }


def test_create_requires_matching_idempotency_transport_fields() -> None:
    spec = load_spec()
    components = spec["components"]
    parameter = components["parameters"]["IdempotencyKey"]
    request = components["schemas"]["PipelineRunCreateRequest"]

    assert parameter["name"] == "Idempotency-Key"
    assert parameter["in"] == "header"
    assert parameter["required"] is True
    assert "idempotency_key" in request["required"]
    assert "match" in parameter["description"].lower()


def test_public_contract_forbids_host_path_fields() -> None:
    spec_text = SPEC_PATH.read_text(encoding="utf-8")
    schemas = load_spec()["components"]["schemas"]

    assert "video_path:" not in spec_text
    assert "output_root:" not in spec_text
    assert "/Users/" not in spec_text
    assert schemas["ArtifactRef"]["properties"]["uri"]["pattern"] == (
        "^artifact://"
    )
    assert "media_id" in schemas["PipelineRunCreateRequest"]["required"]


def test_run_status_exposes_progress_warnings_and_failure_code() -> None:
    schemas = load_spec()["components"]["schemas"]
    run = schemas["PipelineRun"]
    progress = schemas["PipelineProgress"]
    failure = schemas["PipelineFailure"]

    assert {"progress", "warnings"}.issubset(run["required"])
    assert {"planned_stages", "completed_stages", "ratio"}.issubset(
        progress["required"]
    )
    assert "current_stage" in progress["properties"]
    assert "code" in failure["required"]


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


def test_mutating_request_schemas_are_closed_and_versioned() -> None:
    schemas = load_spec()["components"]["schemas"]

    for name in ("PipelineRunCreateRequest", "PipelineQueryRequest"):
        schema = schemas[name]
        assert schema["additionalProperties"] is False
        assert "schema_version" in schema["required"]
        assert schema["properties"]["schema_version"]["const"] == "1"


def test_keyframe_setting_is_bounded_to_adaptive_policy_range() -> None:
    settings = load_spec()["components"]["schemas"]["PipelineSettings"]
    keyframes = settings["properties"]["keyframes_per_scene"]

    assert keyframes["minimum"] == 1
    assert keyframes["maximum"] == 3
