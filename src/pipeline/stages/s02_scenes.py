"""2단계: PySceneDetect로 씬 경계를 검출한다.

출력:
- 02_scenes/scenes.json  : 씬 목록 (id, start/end 초·프레임)
- 02_scenes/scene_stats.csv : 프레임별 content_val 통계 (임계값 튜닝용)
"""

from scenedetect import SceneManager, StatsManager, open_video
from scenedetect.detectors import ContentDetector

from ..context import PipelineContext
from ..logging_setup import stage_logger

NAME = "02_scenes"
OUTPUT = "02_scenes/scenes.json"


def run(ctx: PipelineContext) -> dict:
    log = stage_logger(NAME)
    out_dir = ctx.stage_dir(NAME)

    log.info(
        "씬 검출 시작 (ContentDetector, threshold=%.1f, min_scene_len=%d프레임)",
        ctx.scene_threshold, ctx.min_scene_len_frames,
    )
    video = open_video(str(ctx.video_path))
    stats = StatsManager()
    manager = SceneManager(stats)
    manager.add_detector(
        ContentDetector(
            threshold=ctx.scene_threshold, min_scene_len=ctx.min_scene_len_frames
        )
    )
    manager.detect_scenes(video, show_progress=False)
    scene_list = manager.get_scene_list()

    stats_csv = out_dir / "scene_stats.csv"
    stats.save_to_csv(csv_file=str(stats_csv))
    log.debug("프레임별 통계 저장: %s", stats_csv)

    if not scene_list:
        log.warning("씬 경계 미검출 — 전체 영상을 단일 씬으로 처리합니다")
        duration = video.duration.get_seconds()
        scenes = [{
            "scene_id": 1,
            "start_sec": 0.0,
            "end_sec": round(duration, 3),
            "duration_sec": round(duration, 3),
            "start_frame": 0,
            "end_frame": video.duration.get_frames(),
        }]
    else:
        scenes = []
        for i, (start, end) in enumerate(scene_list, start=1):
            scene = {
                "scene_id": i,
                "start_sec": round(start.get_seconds(), 3),
                "end_sec": round(end.get_seconds(), 3),
                "duration_sec": round(end.get_seconds() - start.get_seconds(), 3),
                "start_frame": start.get_frames(),
                "end_frame": end.get_frames(),
            }
            scenes.append(scene)
            log.debug(
                "씬 %02d: %7.2fs ~ %7.2fs (%.2fs, 프레임 %d~%d)",
                i, scene["start_sec"], scene["end_sec"], scene["duration_sec"],
                scene["start_frame"], scene["end_frame"],
            )

    durations = [s["duration_sec"] for s in scenes]
    log.info(
        "씬 %d개 검출 (평균 %.1fs, 최소 %.1fs, 최대 %.1fs)",
        len(scenes), sum(durations) / len(durations), min(durations), max(durations),
    )

    result = {
        "detector": "ContentDetector",
        "threshold": ctx.scene_threshold,
        "min_scene_len_frames": ctx.min_scene_len_frames,
        "scene_count": len(scenes),
        "scenes": scenes,
    }
    ctx.save_json(out_dir / "scenes.json", result)
    return {"scene_count": len(scenes)}
