"""Shared validation and provenance helpers for selected-scene overlays."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath

from ..context import PipelineContext


OVERLAY_POLICY = "copy-unselected-from-source-v1"
SOURCE_ROOT = "00_source"


def selected_scene_ids(
    ctx: PipelineContext,
    available_scene_ids: Iterable[int],
) -> tuple[int, ...]:
    """Return validated selected IDs or an empty tuple for a normal run."""

    raw = ctx.reprocessing_scene_ids
    if raw in (None, (), []):
        if any(
            value is not None
            for value in (
                ctx.reprocessing_source_run_id,
                ctx.reprocessing_profile,
                ctx.reprocessing_overlay_policy,
            )
        ):
            raise ValueError(
                "reprocessing metadata requires selected scene IDs"
            )
        return ()
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise ValueError("reprocessing_scene_ids must be an array")
    normalized = []
    for value in raw:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(
                "reprocessing_scene_ids must contain positive integers"
            )
        if value in normalized:
            raise ValueError("reprocessing_scene_ids must not contain duplicates")
        normalized.append(value)
    if not ctx.reprocessing_source_run_id:
        raise ValueError("reprocessing_source_run_id is required")
    if not ctx.reprocessing_profile:
        raise ValueError("reprocessing_profile is required")
    if ctx.reprocessing_overlay_policy != OVERLAY_POLICY:
        raise ValueError("unsupported reprocessing overlay policy")
    available = set(available_scene_ids)
    missing = sorted(set(normalized) - available)
    if missing:
        raise ValueError(
            "reprocessing scene IDs are not present in source scenes: "
            + ", ".join(str(value) for value in missing)
        )
    return tuple(normalized)


def source_path(ctx: PipelineContext, relative_path: str) -> Path:
    """Resolve one fixed imported source path below the derived workspace."""

    relative = PurePosixPath(relative_path)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError("reprocessing source path is invalid")
    root = (ctx.out_root / SOURCE_ROOT).resolve()
    target = root.joinpath(*relative.parts).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("reprocessing source path escapes source root") from exc
    return target


def frame_relative_path(value: object) -> PurePosixPath:
    """Validate a keyframe path shared by source and derived workspaces."""

    if not isinstance(value, str) or not value:
        raise ValueError("keyframe path must be a non-empty string")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or relative.parts[:2] != ("03_keyframes", "frames")
        or len(relative.parts) != 3
        or any(part in {"", ".", ".."} for part in relative.parts)
        or relative.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}
    ):
        raise ValueError("keyframe path is outside the frames directory")
    return relative


def provenance(ctx: PipelineContext, origin: str) -> dict[str, object]:
    """Return stable per-entry provenance without host paths."""

    if origin not in {"source", "selected-pass", "full-materialization"}:
        raise ValueError("reprocessing origin is invalid")
    return {
        "origin": origin,
        "source_run_id": ctx.reprocessing_source_run_id,
        "quality_profile": ctx.reprocessing_profile,
        "overlay_policy": ctx.reprocessing_overlay_policy,
    }


def keyed_entries(
    entries: object,
    field_name: str,
) -> dict[tuple[int, int], dict]:
    """Index visual entries by scene/keyframe with duplicate rejection."""

    if not isinstance(entries, list):
        raise ValueError(f"{field_name} must be an array")
    keyed = {}
    positions: dict[int, int] = {}
    for raw in entries:
        if not isinstance(raw, Mapping):
            raise ValueError(f"{field_name} must contain objects")
        try:
            scene_id = int(raw["scene_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} scene_id is invalid") from exc
        positions[scene_id] = positions.get(scene_id, 0) + 1
        index = int(raw.get("keyframe_index", positions[scene_id]))
        key = (scene_id, index)
        if key in keyed:
            raise ValueError(f"{field_name} contains duplicate visual entries")
        keyed[key] = dict(raw)
    return keyed
