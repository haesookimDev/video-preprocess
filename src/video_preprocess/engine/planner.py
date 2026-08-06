"""Deterministic dependency validation and stage selection planning."""

from __future__ import annotations

import heapq
from dataclasses import dataclass

from video_preprocess.domain import StageSpec

from .errors import (
    DependencyCycleError,
    InvalidInputDependencyError,
    PlanSelectionError,
)
from .registry import StageRegistry


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """Topologically ordered Stage specs and inputs crossing its boundary."""

    stages: tuple[StageSpec, ...]
    boundary_inputs: tuple[str, ...]

    @property
    def stage_names(self) -> tuple[str, ...]:
        return tuple(stage.name for stage in self.stages)

    @property
    def outputs(self) -> tuple[str, ...]:
        return tuple(
            output
            for stage in self.stages
            for output in stage.outputs
        )


class DAGPlanner:
    """Validate a registry and build deterministic full or partial plans."""

    def __init__(self, registry: StageRegistry) -> None:
        if not isinstance(registry, StageRegistry):
            raise TypeError("registry must be a StageRegistry")
        self.registry = registry
        self._dependencies = {
            name: set(registry.get(name).dependencies)
            for name in registry.names
        }
        self._dependents = {name: set() for name in registry.names}
        for name, dependencies in self._dependencies.items():
            for dependency in dependencies:
                self._dependents[dependency].add(name)
        self._ordered_names = self._topological_order()
        self._validate_required_inputs()

    @property
    def ordered_stage_names(self) -> tuple[str, ...]:
        return self._ordered_names

    def plan(
        self,
        *,
        stage: str | None = None,
        from_stage: str | None = None,
        to_stage: str | None = None,
    ) -> ExecutionPlan:
        if stage is not None and (
            from_stage is not None or to_stage is not None
        ):
            raise PlanSelectionError(
                "stage cannot be combined with from_stage or to_stage"
            )
        for field_name, value in (
            ("stage", stage),
            ("from_stage", from_stage),
            ("to_stage", to_stage),
        ):
            if value is not None and value not in self.registry.names:
                raise PlanSelectionError(
                    f"{field_name} refers to unknown stage: {value}"
                )

        if stage is not None:
            selected = {stage}
        else:
            selected = set(self._ordered_names)
            if from_stage is not None:
                selected &= self._descendants(from_stage) | {from_stage}
            if to_stage is not None:
                selected &= self._ancestors(to_stage) | {to_stage}
        if not selected:
            raise PlanSelectionError(
                "from_stage and to_stage do not share a dependency path"
            )

        specs = tuple(
            self.registry.get(name)
            for name in self._ordered_names
            if name in selected
        )
        produced = {
            output
            for spec in specs
            for output in spec.outputs
        }
        boundary_inputs = tuple(
            sorted(
                {
                    required_input
                    for spec in specs
                    for required_input in spec.required_inputs
                    if required_input not in produced
                }
            )
        )
        return ExecutionPlan(
            stages=specs,
            boundary_inputs=boundary_inputs,
        )

    def _topological_order(self) -> tuple[str, ...]:
        in_degree = {
            name: len(dependencies)
            for name, dependencies in self._dependencies.items()
        }
        ready = [name for name, degree in in_degree.items() if degree == 0]
        heapq.heapify(ready)
        ordered = []
        while ready:
            name = heapq.heappop(ready)
            ordered.append(name)
            for dependent in sorted(self._dependents[name]):
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    heapq.heappush(ready, dependent)
        if len(ordered) != len(in_degree):
            cycle_members = sorted(
                name for name, degree in in_degree.items() if degree > 0
            )
            raise DependencyCycleError(
                "stage dependency cycle includes: "
                + ", ".join(cycle_members)
            )
        return tuple(ordered)

    def _validate_required_inputs(self) -> None:
        external_inputs = set(self.registry.external_inputs)
        for name in self._ordered_names:
            ancestors = self._ancestors(name)
            for required_input in self.registry.get(name).required_inputs:
                if required_input in external_inputs:
                    continue
                owner = self.registry.output_owner(required_input)
                if owner is None:
                    raise InvalidInputDependencyError(
                        f"{name} requires input {required_input} with no producer"
                    )
                if owner not in ancestors:
                    raise InvalidInputDependencyError(
                        f"{name} requires input {required_input} from {owner}, "
                        "but that stage is not an ancestor"
                    )

    def _ancestors(self, name: str) -> set[str]:
        visited = set()
        pending = list(self._dependencies[name])
        while pending:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            pending.extend(self._dependencies[current])
        return visited

    def _descendants(self, name: str) -> set[str]:
        visited = set()
        pending = list(self._dependents[name])
        while pending:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            pending.extend(self._dependents[current])
        return visited
