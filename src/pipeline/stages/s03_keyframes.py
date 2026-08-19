"""3단계: 씬 길이별 후보에서 시각적으로 중복되지 않은 키프레임을 추출한다.

입력: 02_scenes/scenes.json
출력:
- 03_keyframes/frames/scene_NNN.jpg 또는 scene_NNN_II.jpg
- 03_keyframes/keyframes.json
"""

import math
import shutil
import statistics
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from ..context import PipelineContext
from ..logging_setup import stage_logger
from ._reprocessing import (
    frame_relative_path,
    provenance,
    selected_scene_ids,
    source_path,
)

NAME = "03_keyframes"
OUTPUT = "03_keyframes/keyframes.json"
SELECTION_POLICY = "duration-adaptive-v1"
DURATION_THRESHOLDS_SEC = (8.0, 20.0)
TIMESTAMP_STRATEGY = "evenly_spaced_interior_points"
DEDUPLICATION_ALGORITHM = "phash-64-dct-v1"
HASH_BITS = 64
HASH_IMAGE_SIZE = 32
HASH_LOW_FREQUENCY_SIZE = 8
HAMMING_DISTANCE_THRESHOLD = 6
DEDUPLICATION_SCOPE = "within_scene"
COMPARISON_ORDER = "timestamp_ascending_against_retained"
REMOVAL_REASON = "perceptual_hash_distance_lte_threshold"
_DCT_COSINES = tuple(
    tuple(
        math.cos(
            math.pi
            * (2 * position + 1)
            * frequency
            / (2 * HASH_IMAGE_SIZE)
        )
        for position in range(HASH_IMAGE_SIZE)
    )
    for frequency in range(HASH_LOW_FREQUENCY_SIZE)
)


@dataclass(frozen=True, slots=True)
class _CandidateFrame:
    scene_id: int
    candidate_index: int
    candidate_count: int
    timestamp_sec: float
    path: Path
    perceptual_hash: str


@dataclass(frozen=True, slots=True)
class _RemovedFrame:
    candidate: _CandidateFrame
    duplicate_of: _CandidateFrame
    hamming_distance: int


def _adaptive_keyframe_count(
    duration_sec: float,
    max_keyframes_per_scene: int,
) -> int:
    """Return the duration-derived count capped by the configured maximum."""

    if duration_sec < DURATION_THRESHOLDS_SEC[0]:
        adaptive_count = 1
    elif duration_sec < DURATION_THRESHOLDS_SEC[1]:
        adaptive_count = 2
    else:
        adaptive_count = 3
    return min(adaptive_count, max_keyframes_per_scene)


def _interior_timestamps(
    start_sec: float,
    end_sec: float,
    count: int,
) -> tuple[float, ...]:
    """Return stable interior points that never select a scene boundary."""

    step = (end_sec - start_sec) / (count + 1)
    return tuple(
        round(start_sec + step * index, 3)
        for index in range(1, count + 1)
    )


def _frame_name(scene_id: int, index: int, count: int) -> str:
    """Keep the legacy filename for a one-frame scene."""

    if count == 1:
        return f"scene_{scene_id:03d}.jpg"
    return f"scene_{scene_id:03d}_{index:02d}.jpg"


def _perceptual_hash(path: Path) -> str:
    """Return a deterministic 64-bit DCT perceptual hash for one image."""

    with Image.open(path) as source:
        grayscale = source.convert("L").resize(
            (HASH_IMAGE_SIZE, HASH_IMAGE_SIZE),
            Image.Resampling.LANCZOS,
        )
        flattened = getattr(grayscale, "get_flattened_data", None)
        pixel_values = (
            flattened() if flattened is not None else grayscale.getdata()
        )
        pixels = tuple(float(value) for value in pixel_values)

    coefficients = []
    for vertical_frequency in range(HASH_LOW_FREQUENCY_SIZE):
        vertical_cosines = _DCT_COSINES[vertical_frequency]
        for horizontal_frequency in range(HASH_LOW_FREQUENCY_SIZE):
            horizontal_cosines = _DCT_COSINES[horizontal_frequency]
            coefficient = 0.0
            for y in range(HASH_IMAGE_SIZE):
                row_offset = y * HASH_IMAGE_SIZE
                row_total = sum(
                    pixels[row_offset + x] * horizontal_cosines[x]
                    for x in range(HASH_IMAGE_SIZE)
                )
                coefficient += row_total * vertical_cosines[y]
            coefficients.append(coefficient)

    median = statistics.median(coefficients[1:])
    value = 0
    for coefficient in coefficients:
        value = (value << 1) | int(coefficient > median)
    return f"{value:0{HASH_BITS // 4}x}"


def _hamming_distance(first: str, second: str) -> int:
    return (int(first, 16) ^ int(second, 16)).bit_count()


def _deduplicate_candidates(
    candidates: list[_CandidateFrame],
) -> tuple[list[_CandidateFrame], list[_RemovedFrame]]:
    """Greedily retain chronological candidates outside the hash threshold."""

    retained: list[_CandidateFrame] = []
    removed: list[_RemovedFrame] = []
    for candidate in candidates:
        if not retained:
            retained.append(candidate)
            continue
        distance, _, duplicate_of = min(
            (
                _hamming_distance(
                    candidate.perceptual_hash,
                    retained_candidate.perceptual_hash,
                ),
                retained_candidate.candidate_index,
                retained_candidate,
            )
            for retained_candidate in retained
        )
        if distance <= HAMMING_DISTANCE_THRESHOLD:
            removed.append(
                _RemovedFrame(
                    candidate=candidate,
                    duplicate_of=duplicate_of,
                    hamming_distance=distance,
                )
            )
        else:
            retained.append(candidate)
    return retained, removed


def _merge_source_overlay(
    ctx: PipelineContext,
    scenes: list[dict],
    selected_ids: tuple[int, ...],
    selected_keyframes: list[dict],
    selected_removed: list[dict],
    selected_statistics: list[dict],
    selected_frame_paths: set[Path],
) -> tuple[list[dict], list[dict], list[dict]]:
    """Merge new selected scenes with immutable first-pass visual output."""

    source_payload = ctx.load_json(
        source_path(ctx, "03_keyframes/keyframes.json")
    )
    source_keyframes = source_payload.get("keyframes")
    source_dedup = source_payload.get("deduplication")
    if not isinstance(source_keyframes, list) or not isinstance(
        source_dedup, dict
    ):
        raise ValueError("source keyframe overlay document is invalid")
    selected_set = set(selected_ids)
    scene_order = [int(scene["scene_id"]) for scene in scenes]
    source_by_scene: dict[int, list[dict]] = {}
    for entry in source_keyframes:
        if not isinstance(entry, dict):
            raise ValueError("source keyframes must contain objects")
        scene_id = int(entry["scene_id"])
        source_by_scene.setdefault(scene_id, []).append(entry)
    selected_by_scene: dict[int, list[dict]] = {}
    for entry in selected_keyframes:
        selected_by_scene.setdefault(int(entry["scene_id"]), []).append(entry)
    if set(selected_by_scene) != selected_set:
        raise ValueError("selected keyframe output does not cover every scene")

    combined = []
    for scene_id in scene_order:
        if scene_id in selected_set:
            entries = selected_by_scene[scene_id]
            for entry in entries:
                combined.append({
                    **entry,
                    "reprocessing": provenance(ctx, "selected-pass"),
                })
            continue
        entries = source_by_scene.get(scene_id, [])
        if not entries:
            raise ValueError(
                f"source keyframes do not cover scene {scene_id}"
            )
        for entry in entries:
            relative = frame_relative_path(entry.get("path"))
            source_frame = source_path(ctx, relative.as_posix())
            if not source_frame.is_file():
                raise ValueError("source keyframe image is missing")
            target = ctx.out_root.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_frame, target)
            selected_frame_paths.add(target)
            combined.append({
                **entry,
                "size_bytes": target.stat().st_size,
                "reprocessing": provenance(ctx, "source"),
            })

    source_statistics = source_dedup.get("scene_statistics")
    source_removed = source_dedup.get("removed")
    if not isinstance(source_statistics, list) or not isinstance(
        source_removed, list
    ):
        raise ValueError("source keyframe deduplication metadata is invalid")
    statistics_by_scene = {
        int(item["scene_id"]): dict(item)
        for item in source_statistics
        if isinstance(item, dict) and "scene_id" in item
    }
    statistics_by_scene.update({
        int(item["scene_id"]): dict(item)
        for item in selected_statistics
    })
    if any(scene_id not in statistics_by_scene for scene_id in scene_order):
        raise ValueError("source keyframe statistics do not cover every scene")
    combined_statistics = [
        statistics_by_scene[scene_id] for scene_id in scene_order
    ]
    combined_removed = [
        {
            **item,
            "reprocessing": provenance(ctx, "source"),
        }
        for item in source_removed
        if isinstance(item, dict)
        and int(item.get("scene_id", -1)) not in selected_set
    ] + [
        {
            **item,
            "reprocessing": provenance(ctx, "selected-pass"),
        }
        for item in selected_removed
    ]
    return combined, combined_removed, combined_statistics


def run(ctx: PipelineContext) -> dict:
    log = stage_logger(NAME)
    out_dir = ctx.stage_dir(NAME)
    frames_dir = out_dir / "frames"
    frames_dir.mkdir(exist_ok=True)

    scenes_path = ctx.out_root / "02_scenes" / "scenes.json"
    if ctx.reprocessing_scene_ids:
        scenes_path = source_path(ctx, "02_scenes/scenes.json")
    scenes = ctx.load_json(scenes_path)["scenes"]
    reprocessing_scene_ids = selected_scene_ids(
        ctx,
        (int(scene["scene_id"]) for scene in scenes),
    )
    selected_set = set(reprocessing_scene_ids)
    processing_scenes = (
        [scene for scene in scenes if int(scene["scene_id"]) in selected_set]
        if reprocessing_scene_ids
        else scenes
    )
    max_keyframes = ctx.keyframes_per_scene
    if (
        isinstance(max_keyframes, bool)
        or not isinstance(max_keyframes, int)
        or not 1 <= max_keyframes <= 3
    ):
        raise ValueError("keyframes_per_scene must be between 1 and 3")
    log.info(
        "씬 %d개에서 adaptive 키프레임 추출 시작 (씬당 최대 %d장)",
        len(processing_scenes),
        max_keyframes,
    )

    keyframes = []
    removed_keyframes = []
    scene_statistics = []
    selected_frame_paths = set()
    candidate_keyframe_count = 0
    with tempfile.TemporaryDirectory(
        dir=out_dir,
        prefix=".keyframe-candidates-",
    ) as temporary_directory:
        candidate_dir = Path(temporary_directory)
        scene_selections = []
        for scene in processing_scenes:
            scene_id = int(scene["scene_id"])
            start_sec = float(scene["start_sec"])
            end_sec = float(scene["end_sec"])
            duration_sec = end_sec - start_sec
            if duration_sec <= 0:
                raise ValueError(
                    f"scene {scene_id} must have a positive duration"
                )
            frame_count = _adaptive_keyframe_count(
                duration_sec,
                max_keyframes,
            )
            timestamps = _interior_timestamps(
                start_sec,
                end_sec,
                frame_count,
            )
            candidates = []
            for candidate_index, timestamp_sec in enumerate(
                timestamps,
                start=1,
            ):
                candidate_path = candidate_dir / (
                    f"scene_{scene_id:03d}_{candidate_index:02d}.jpg"
                )
                cmd = [
                    "ffmpeg", "-v", "error", "-ss", f"{timestamp_sec:.3f}",
                    "-i", str(ctx.video_path),
                    "-frames:v", "1", "-q:v", "2", "-y",
                    str(candidate_path),
                ]
                log.debug(
                    "씬 %02d 후보 %d/%d: t=%.3fs",
                    scene_id,
                    candidate_index,
                    frame_count,
                    timestamp_sec,
                )
                subprocess.run(cmd, capture_output=True, check=True)
                candidates.append(
                    _CandidateFrame(
                        scene_id=scene_id,
                        candidate_index=candidate_index,
                        candidate_count=frame_count,
                        timestamp_sec=timestamp_sec,
                        path=candidate_path,
                        perceptual_hash=_perceptual_hash(candidate_path),
                    )
                )
            retained, removed = _deduplicate_candidates(candidates)
            candidate_keyframe_count += len(candidates)
            scene_selections.append((scene_id, retained, removed))

        for scene_id, retained, removed in scene_selections:
            retained_count = len(retained)
            retained_metadata = {}
            for keyframe_index, candidate in enumerate(retained, start=1):
                frame_path = frames_dir / _frame_name(
                    scene_id,
                    keyframe_index,
                    retained_count,
                )
                candidate.path.replace(frame_path)
                selected_frame_paths.add(frame_path)
                relative_path = str(frame_path.relative_to(ctx.out_root))
                retained_metadata[candidate.candidate_index] = (
                    keyframe_index,
                    relative_path,
                )
                keyframes.append({
                    "scene_id": scene_id,
                    "keyframe_index": keyframe_index,
                    "keyframe_count": retained_count,
                    "timestamp_sec": candidate.timestamp_sec,
                    "path": relative_path,
                    "size_bytes": frame_path.stat().st_size,
                    "perceptual_hash": candidate.perceptual_hash,
                })
            for removal in removed:
                duplicate_index, duplicate_path = retained_metadata[
                    removal.duplicate_of.candidate_index
                ]
                removed_keyframes.append({
                    "scene_id": scene_id,
                    "candidate_index": removal.candidate.candidate_index,
                    "candidate_count": removal.candidate.candidate_count,
                    "timestamp_sec": removal.candidate.timestamp_sec,
                    "perceptual_hash": removal.candidate.perceptual_hash,
                    "duplicate_of_keyframe_index": duplicate_index,
                    "duplicate_of_timestamp_sec": (
                        removal.duplicate_of.timestamp_sec
                    ),
                    "duplicate_of_path": duplicate_path,
                    "hamming_distance": removal.hamming_distance,
                    "reason": REMOVAL_REASON,
                })
            scene_statistics.append({
                "scene_id": scene_id,
                "candidate_count": len(retained) + len(removed),
                "retained_count": retained_count,
                "removed_count": len(removed),
            })

    processed_candidate_count = candidate_keyframe_count
    if reprocessing_scene_ids:
        keyframes, removed_keyframes, scene_statistics = (
            _merge_source_overlay(
                ctx,
                scenes,
                reprocessing_scene_ids,
                keyframes,
                removed_keyframes,
                scene_statistics,
                selected_frame_paths,
            )
        )
        candidate_keyframe_count = sum(
            int(item["candidate_count"]) for item in scene_statistics
        )

    for stale_path in frames_dir.glob("scene_*.jpg"):
        if stale_path not in selected_frame_paths:
            stale_path.unlink()

    total_kb = sum(k["size_bytes"] for k in keyframes) / 1024
    log.info(
        "키프레임 후보 %d장 중 %d장 보존, %d장 제거 (총 %.1fKB)",
        candidate_keyframe_count,
        len(keyframes),
        len(removed_keyframes),
        total_kb,
    )

    payload = {
        "selection_policy": {
            "name": SELECTION_POLICY,
            "max_keyframes_per_scene": max_keyframes,
            "duration_thresholds_sec": list(DURATION_THRESHOLDS_SEC),
            "timestamp_strategy": TIMESTAMP_STRATEGY,
        },
        "deduplication": {
            "algorithm": DEDUPLICATION_ALGORITHM,
            "hash_bits": HASH_BITS,
            "hamming_distance_threshold": HAMMING_DISTANCE_THRESHOLD,
            "comparison_scope": DEDUPLICATION_SCOPE,
            "comparison_order": COMPARISON_ORDER,
            "minimum_retained_per_scene": 1,
            "candidate_count": candidate_keyframe_count,
            "retained_count": len(keyframes),
            "removed_count": len(removed_keyframes),
            "scene_statistics": scene_statistics,
            "removed": removed_keyframes,
        },
        "keyframes": keyframes,
    }
    if reprocessing_scene_ids:
        payload["reprocessing"] = {
            **provenance(ctx, "selected-pass"),
            "selected_scene_ids": list(reprocessing_scene_ids),
            "processed_scene_count": len(reprocessing_scene_ids),
            "reused_scene_count": len(scenes) - len(reprocessing_scene_ids),
        }
    ctx.save_json(out_dir / "keyframes.json", payload)
    metrics = {
        "candidate_keyframe_count": candidate_keyframe_count,
        "keyframe_count": len(keyframes),
        "removed_keyframe_count": len(removed_keyframes),
        "scene_count": len(scenes),
    }
    if reprocessing_scene_ids:
        metrics.update({
            "processed_candidate_keyframe_count": processed_candidate_count,
            "processed_scene_count": len(reprocessing_scene_ids),
            "reused_scene_count": len(scenes) - len(reprocessing_scene_ids),
        })
    return metrics
