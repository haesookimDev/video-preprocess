"""Tests for immutable StageSpec registry validation."""

import pytest

from video_preprocess.domain import StageSpec
from video_preprocess.engine import (
    DuplicateOutputError,
    DuplicateStageError,
    StageRegistry,
    UnknownDependencyError,
)


def test_registry_indexes_specs_independent_of_registration_order() -> None:
    second = StageSpec(
        name="02_second",
        stage_version="1.0.0",
        dependencies=("01_first",),
        required_inputs=("first_output",),
        outputs=("second_output",),
    )
    first = StageSpec(
        name="01_first",
        stage_version="1.0.0",
        required_inputs=("source",),
        outputs=("first_output",),
    )

    registry = StageRegistry(
        [second, first],
        external_inputs=("source",),
    )

    assert registry.names == ("01_first", "02_second")
    assert tuple(spec.name for spec in registry) == registry.names
    assert registry.get("02_second") is second
    assert registry.output_owner("first_output") == "01_first"
    assert registry.external_inputs == ("source",)


def test_registry_rejects_duplicate_stage_names() -> None:
    specs = [
        StageSpec(name="stage", stage_version="1", outputs=("one",)),
        StageSpec(name="stage", stage_version="2", outputs=("two",)),
    ]

    with pytest.raises(DuplicateStageError, match="duplicate stage"):
        StageRegistry(specs)


def test_registry_rejects_duplicate_output_owners() -> None:
    specs = [
        StageSpec(name="one", stage_version="1", outputs=("shared",)),
        StageSpec(name="two", stage_version="1", outputs=("shared",)),
    ]

    with pytest.raises(DuplicateOutputError, match="shared"):
        StageRegistry(specs)


def test_registry_rejects_output_conflicting_with_external_input() -> None:
    spec = StageSpec(
        name="one",
        stage_version="1",
        outputs=("video",),
    )

    with pytest.raises(DuplicateOutputError, match="external input"):
        StageRegistry([spec], external_inputs=("video",))


def test_registry_rejects_unknown_dependency() -> None:
    spec = StageSpec(
        name="two",
        stage_version="1",
        dependencies=("missing",),
    )

    with pytest.raises(UnknownDependencyError, match="missing"):
        StageRegistry([spec])


def test_registry_rejects_non_stage_spec_entry() -> None:
    with pytest.raises(TypeError, match="StageSpec"):
        StageRegistry([object()])


def test_registry_get_reports_unknown_stage() -> None:
    registry = StageRegistry([])

    with pytest.raises(KeyError, match="unknown stage"):
        registry.get("missing")
