"""3단계: 씬 길이에 따라 대표 키프레임을 1~3장 추출한다.

입력: 02_scenes/scenes.json
출력:
- 03_keyframes/frames/scene_NNN.jpg 또는 scene_NNN_II.jpg
- 03_keyframes/keyframes.json
"""

import subprocess

from ..context import PipelineContext
from ..logging_setup import stage_logger

NAME = "03_keyframes"
OUTPUT = "03_keyframes/keyframes.json"
SELECTION_POLICY = "duration-adaptive-v1"
DURATION_THRESHOLDS_SEC = (8.0, 20.0)
TIMESTAMP_STRATEGY = "evenly_spaced_interior_points"


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


def run(ctx: PipelineContext) -> dict:
    log = stage_logger(NAME)
    out_dir = ctx.stage_dir(NAME)
    frames_dir = out_dir / "frames"
    frames_dir.mkdir(exist_ok=True)

    scenes = ctx.load_json(ctx.out_root / "02_scenes" / "scenes.json")["scenes"]
    max_keyframes = ctx.keyframes_per_scene
    if (
        isinstance(max_keyframes, bool)
        or not isinstance(max_keyframes, int)
        or not 1 <= max_keyframes <= 3
    ):
        raise ValueError("keyframes_per_scene must be between 1 and 3")
    log.info(
        "씬 %d개에서 adaptive 키프레임 추출 시작 (씬당 최대 %d장)",
        len(scenes),
        max_keyframes,
    )

    keyframes = []
    selected_frame_paths = set()
    for scene in scenes:
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
        for keyframe_index, timestamp_sec in enumerate(timestamps, start=1):
            frame_path = frames_dir / _frame_name(
                scene_id,
                keyframe_index,
                frame_count,
            )
            cmd = [
                "ffmpeg", "-v", "error", "-ss", f"{timestamp_sec:.3f}",
                "-i", str(ctx.video_path),
                "-frames:v", "1", "-q:v", "2", "-y", str(frame_path),
            ]
            log.debug(
                "씬 %02d 키프레임 %d/%d: t=%.3fs → %s",
                scene_id,
                keyframe_index,
                frame_count,
                timestamp_sec,
                frame_path.name,
            )
            subprocess.run(cmd, capture_output=True, check=True)
            selected_frame_paths.add(frame_path)
            keyframes.append({
                "scene_id": scene_id,
                "keyframe_index": keyframe_index,
                "keyframe_count": frame_count,
                "timestamp_sec": timestamp_sec,
                "path": str(frame_path.relative_to(ctx.out_root)),
                "size_bytes": frame_path.stat().st_size,
            })

    for stale_path in frames_dir.glob("scene_*.jpg"):
        if stale_path not in selected_frame_paths:
            stale_path.unlink()

    total_kb = sum(k["size_bytes"] for k in keyframes) / 1024
    log.info("키프레임 %d장 추출 완료 (총 %.1fKB)", len(keyframes), total_kb)

    ctx.save_json(
        out_dir / "keyframes.json",
        {
            "selection_policy": {
                "name": SELECTION_POLICY,
                "max_keyframes_per_scene": max_keyframes,
                "duration_thresholds_sec": list(DURATION_THRESHOLDS_SEC),
                "timestamp_strategy": TIMESTAMP_STRATEGY,
            },
            "keyframes": keyframes,
        },
    )
    return {"keyframe_count": len(keyframes), "scene_count": len(scenes)}
