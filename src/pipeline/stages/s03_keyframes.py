"""3단계: 씬별 대표 키프레임을 추출한다 (씬 중앙 프레임, ffmpeg 시크).

입력: 02_scenes/scenes.json
출력:
- 03_keyframes/frames/scene_NNN.jpg
- 03_keyframes/keyframes.json
"""

import subprocess

from ..context import PipelineContext
from ..logging_setup import stage_logger

NAME = "03_keyframes"
OUTPUT = "03_keyframes/keyframes.json"


def run(ctx: PipelineContext) -> dict:
    log = stage_logger(NAME)
    out_dir = ctx.stage_dir(NAME)
    frames_dir = out_dir / "frames"
    frames_dir.mkdir(exist_ok=True)

    scenes = ctx.load_json(ctx.out_root / "02_scenes" / "scenes.json")["scenes"]
    log.info("씬 %d개에서 키프레임 추출 시작 (씬당 %d장)",
             len(scenes), ctx.keyframes_per_scene)

    keyframes = []
    for scene in scenes:
        mid = (scene["start_sec"] + scene["end_sec"]) / 2
        frame_path = frames_dir / f"scene_{scene['scene_id']:03d}.jpg"
        cmd = [
            "ffmpeg", "-v", "error", "-ss", f"{mid:.3f}",
            "-i", str(ctx.video_path),
            "-frames:v", "1", "-q:v", "2", "-y", str(frame_path),
        ]
        log.debug("씬 %02d: t=%.3fs → %s", scene["scene_id"], mid, frame_path.name)
        subprocess.run(cmd, capture_output=True, check=True)
        keyframes.append({
            "scene_id": scene["scene_id"],
            "timestamp_sec": round(mid, 3),
            "path": str(frame_path.relative_to(ctx.out_root)),
            "size_bytes": frame_path.stat().st_size,
        })

    total_kb = sum(k["size_bytes"] for k in keyframes) / 1024
    log.info("키프레임 %d장 추출 완료 (총 %.1fKB)", len(keyframes), total_kb)

    ctx.save_json(out_dir / "keyframes.json", {"keyframes": keyframes})
    return {"keyframe_count": len(keyframes)}
