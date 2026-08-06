"""Tests for deterministic DAG validation and partial planning."""

import pytest

from video_preprocess.domain import StageSpec
from video_preprocess.engine import (
    DAGPlanner,
    DependencyCycleError,
    InvalidInputDependencyError,
    PlanSelectionError,
    StageRegistry,
    create_default_registry,
)


EXPECTED_ORDER = (
    "01_probe",
    "02_scenes",
    "03_keyframes",
    "04_audio",
    "05_vad",
    "06_stt",
    "07_diarize",
    "08_captions",
    "09_timeline",
    "10_index",
    "11_context",
)


def test_default_registry_has_stable_eleven_stage_plan() -> None:
    registry = create_default_registry()
    planner = DAGPlanner(registry)

    plan = planner.plan()

    assert len(registry) == 11
    assert planner.ordered_stage_names == EXPECTED_ORDER
    assert plan.stage_names == EXPECTED_ORDER
    assert plan.boundary_inputs == ("video",)
    assert registry.get("05_vad").model_slots == ("vad",)
    assert registry.get("06_stt").model_slots == ("stt",)
    assert registry.get("07_diarize").dependencies == ("04_audio",)
    assert registry.get("08_captions").dependencies == ("03_keyframes",)


def test_topological_order_is_independent_of_registration_order() -> None:
    specs = [
        StageSpec(
            name="c",
            stage_version="1",
            dependencies=("a",),
            required_inputs=("a_out",),
            outputs=("c_out",),
        ),
        StageSpec(
            name="b",
            stage_version="1",
            dependencies=("a",),
            required_inputs=("a_out",),
            outputs=("b_out",),
        ),
        StageSpec(
            name="a",
            stage_version="1",
            outputs=("a_out",),
        ),
    ]

    planner = DAGPlanner(StageRegistry(specs))

    assert planner.ordered_stage_names == ("a", "b", "c")


def test_planner_rejects_dependency_cycle() -> None:
    specs = [
        StageSpec(
            name="a",
            stage_version="1",
            dependencies=("c",),
        ),
        StageSpec(
            name="b",
            stage_version="1",
            dependencies=("a",),
        ),
        StageSpec(
            name="c",
            stage_version="1",
            dependencies=("b",),
        ),
    ]

    with pytest.raises(DependencyCycleError, match="a, b, c"):
        DAGPlanner(StageRegistry(specs))


def test_planner_rejects_required_input_without_producer() -> None:
    registry = StageRegistry(
        [
            StageSpec(
                name="stage",
                stage_version="1",
                required_inputs=("missing",),
            )
        ]
    )

    with pytest.raises(InvalidInputDependencyError, match="no producer"):
        DAGPlanner(registry)


def test_planner_rejects_input_producer_that_is_not_an_ancestor() -> None:
    registry = StageRegistry(
        [
            StageSpec(
                name="producer",
                stage_version="1",
                outputs=("data",),
            ),
            StageSpec(
                name="consumer",
                stage_version="1",
                required_inputs=("data",),
            ),
        ]
    )

    with pytest.raises(InvalidInputDependencyError, match="not an ancestor"):
        DAGPlanner(registry)


def test_exact_stage_plan_exposes_upstream_boundary_inputs() -> None:
    planner = DAGPlanner(create_default_registry())

    plan = planner.plan(stage="06_stt")

    assert plan.stage_names == ("06_stt",)
    assert plan.boundary_inputs == ("audio", "vad_segments")
    assert plan.outputs == ("transcript",)


def test_from_stage_selects_all_descendants_in_topological_order() -> None:
    planner = DAGPlanner(create_default_registry())

    plan = planner.plan(from_stage="06_stt")

    assert plan.stage_names == (
        "06_stt",
        "09_timeline",
        "10_index",
        "11_context",
    )
    assert plan.boundary_inputs == (
        "audio",
        "captions",
        "diarization",
        "keyframes",
        "metadata",
        "scenes",
        "vad_segments",
    )


def test_to_stage_selects_all_ancestors() -> None:
    planner = DAGPlanner(create_default_registry())

    plan = planner.plan(to_stage="09_timeline")

    assert plan.stage_names == EXPECTED_ORDER[:9]
    assert plan.boundary_inputs == ("video",)


def test_from_and_to_stage_select_dependency_path_intersection() -> None:
    planner = DAGPlanner(create_default_registry())

    plan = planner.plan(from_stage="04_audio", to_stage="09_timeline")

    assert plan.stage_names == (
        "04_audio",
        "05_vad",
        "06_stt",
        "07_diarize",
        "09_timeline",
    )
    assert plan.boundary_inputs == (
        "captions",
        "keyframes",
        "metadata",
        "scenes",
        "video",
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"stage": "missing"},
        {"from_stage": "missing"},
        {"to_stage": "missing"},
    ],
)
def test_plan_rejects_unknown_selection(kwargs) -> None:
    planner = DAGPlanner(create_default_registry())

    with pytest.raises(PlanSelectionError, match="unknown stage"):
        planner.plan(**kwargs)


def test_plan_rejects_exact_stage_combined_with_range() -> None:
    planner = DAGPlanner(create_default_registry())

    with pytest.raises(PlanSelectionError, match="cannot be combined"):
        planner.plan(stage="05_vad", to_stage="09_timeline")


def test_plan_rejects_range_without_dependency_path() -> None:
    planner = DAGPlanner(create_default_registry())

    with pytest.raises(PlanSelectionError, match="dependency path"):
        planner.plan(from_stage="10_index", to_stage="09_timeline")
