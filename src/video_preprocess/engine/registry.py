"""Validated immutable registry of pipeline StageSpec metadata."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping

from video_preprocess.domain import StageSpec

from .errors import (
    DuplicateOutputError,
    DuplicateStageError,
    UnknownDependencyError,
)


class StageRegistry:
    """Index StageSpec objects and reject ambiguous graph metadata."""

    def __init__(
        self,
        specs: Iterable[StageSpec],
        *,
        external_inputs: Iterable[str] = (),
    ) -> None:
        by_name: dict[str, StageSpec] = {}
        for spec in specs:
            if not isinstance(spec, StageSpec):
                raise TypeError("registry entries must be StageSpec instances")
            if spec.name in by_name:
                raise DuplicateStageError(
                    f"duplicate stage name: {spec.name}"
                )
            by_name[spec.name] = spec

        normalized_external_inputs = []
        seen_external_inputs = set()
        for input_name in external_inputs:
            if not isinstance(input_name, str) or not input_name.strip():
                raise ValueError(
                    "external input keys must be non-empty strings"
                )
            normalized = input_name.strip()
            if normalized in seen_external_inputs:
                raise ValueError(
                    f"duplicate external input key: {normalized}"
                )
            normalized_external_inputs.append(normalized)
            seen_external_inputs.add(normalized)

        output_owners: dict[str, str] = {}
        for stage_name in sorted(by_name):
            spec = by_name[stage_name]
            for dependency in spec.dependencies:
                if dependency not in by_name:
                    raise UnknownDependencyError(
                        f"{stage_name} depends on unknown stage {dependency}"
                    )
            for output in spec.outputs:
                owner = output_owners.get(output)
                if owner is not None:
                    raise DuplicateOutputError(
                        f"output {output} is produced by both "
                        f"{owner} and {stage_name}"
                    )
                if output in seen_external_inputs:
                    raise DuplicateOutputError(
                        f"output {output} conflicts with an external input"
                    )
                output_owners[output] = stage_name

        self._by_name = by_name
        self._external_inputs = tuple(sorted(normalized_external_inputs))
        self._output_owners = output_owners

    def __len__(self) -> int:
        return len(self._by_name)

    def __iter__(self) -> Iterator[StageSpec]:
        for name in sorted(self._by_name):
            yield self._by_name[name]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_name))

    @property
    def external_inputs(self) -> tuple[str, ...]:
        return self._external_inputs

    @property
    def output_owners(self) -> Mapping[str, str]:
        return dict(self._output_owners)

    def get(self, name: str) -> StageSpec:
        try:
            return self._by_name[name]
        except KeyError as exc:
            raise KeyError(f"unknown stage: {name}") from exc

    def output_owner(self, output: str) -> str | None:
        return self._output_owners.get(output)
