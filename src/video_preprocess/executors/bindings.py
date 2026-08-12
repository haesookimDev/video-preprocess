"""Stable Stage name to injected execution callable bindings."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable

from video_preprocess.domain import StageResult, StageTask

from .contracts import ExecutionControl

from .errors import DuplicateStageBindingError, UnknownStageBindingError


StageRunner = (
    Callable[[StageTask], StageResult | Awaitable[StageResult]]
    | Callable[
        [StageTask, ExecutionControl],
        StageResult | Awaitable[StageResult],
    ]
)


class StageBindingRegistry:
    """Keep concrete Stage runners outside StageSpec planning metadata."""

    def __init__(
        self,
        bindings: Iterable[tuple[str, StageRunner]],
    ) -> None:
        by_name = {}
        for stage_name, runner in bindings:
            if not isinstance(stage_name, str) or not stage_name.strip():
                raise ValueError("stage binding name must be non-empty")
            normalized_name = stage_name.strip()
            if normalized_name in by_name:
                raise DuplicateStageBindingError(
                    f"duplicate stage binding: {normalized_name}"
                )
            if not callable(runner):
                raise TypeError(
                    f"stage runner must be callable: {normalized_name}"
                )
            by_name[normalized_name] = runner
        self._by_name = by_name

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_name))

    def get(self, stage_name: str) -> StageRunner:
        try:
            return self._by_name[stage_name]
        except KeyError as exc:
            raise UnknownStageBindingError(
                f"no runner is bound for stage: {stage_name}"
            ) from exc
