"""Tests for deterministic duration-adaptive keyframe selection."""

import json
from pathlib import Path

import pytest

from pipeline.context import PipelineContext
from pipeline.stages import s03_keyframes
from video_preprocess.services import PipelineSettings


def test_adaptive_count_uses_duration_thresholds_and_maximum() -> None:
    assert s03_keyframes._adaptive_keyframe_count(7.999, 3) == 1
    assert s03_keyframes._adaptive_keyframe_count(8.0, 3) == 2
    assert s03_keyframes._adaptive_keyframe_count(19.999, 3) == 2
    assert s03_keyframes._adaptive_keyframe_count(20.0, 3) == 3
    assert s03_keyframes._adaptive_keyframe_count(30.0, 2) == 2
    assert s03_keyframes._adaptive_keyframe_count(30.0, 1) == 1


@pytest.mark.parametrize("value", [True, 0, 4, 1.5])
def test_pipeline_settings_reject_invalid_keyframe_maximum(value) -> None:
    with pytest.raises(ValueError, match="keyframes_per_scene"):
        PipelineSettings(keyframes_per_scene=value)


def test_stage_extracts_one_to_three_evenly_spaced_keyframes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    context = PipelineContext(
        video_path=tmp_path / "sample.mp4",
        out_root=tmp_path / "output" / "sample",
        keyframes_per_scene=3,
    )
    context.video_path.write_bytes(b"video")
    context.save_json(
        context.stage_dir("02_scenes") / "scenes.json",
        {
            "scenes": [
                {
                    "scene_id": 1,
                    "start_sec": 0.0,
                    "end_sec": 6.0,
                    "duration_sec": 6.0,
                },
                {
                    "scene_id": 2,
                    "start_sec": 6.0,
                    "end_sec": 16.0,
                    "duration_sec": 10.0,
                },
                {
                    "scene_id": 3,
                    "start_sec": 16.0,
                    "end_sec": 40.0,
                    "duration_sec": 24.0,
                },
            ]
        },
    )
    commands = []

    def fake_run(command, *, capture_output, check):
        assert capture_output is True
        assert check is True
        commands.append(command)
        Path(command[-1]).write_bytes(f"image-{len(commands)}".encode())

    monkeypatch.setattr(s03_keyframes.subprocess, "run", fake_run)

    metrics = s03_keyframes.run(context)

    payload = json.loads(
        (context.out_root / "03_keyframes" / "keyframes.json").read_text(
            encoding="utf-8"
        )
    )
    assert metrics == {"keyframe_count": 6, "scene_count": 3}
    assert payload["selection_policy"] == {
        "name": "duration-adaptive-v1",
        "max_keyframes_per_scene": 3,
        "duration_thresholds_sec": [8.0, 20.0],
        "timestamp_strategy": "evenly_spaced_interior_points",
    }
    assert [entry["timestamp_sec"] for entry in payload["keyframes"]] == [
        3.0,
        9.333,
        12.667,
        22.0,
        28.0,
        34.0,
    ]
    assert [entry["path"] for entry in payload["keyframes"]] == [
        "03_keyframes/frames/scene_001.jpg",
        "03_keyframes/frames/scene_002_01.jpg",
        "03_keyframes/frames/scene_002_02.jpg",
        "03_keyframes/frames/scene_003_01.jpg",
        "03_keyframes/frames/scene_003_02.jpg",
        "03_keyframes/frames/scene_003_03.jpg",
    ]
    assert [
        (entry["keyframe_index"], entry["keyframe_count"])
        for entry in payload["keyframes"]
    ] == [(1, 1), (1, 2), (2, 2), (1, 3), (2, 3), (3, 3)]


def test_single_keyframe_keeps_legacy_midpoint_filename(
    tmp_path: Path,
    monkeypatch,
) -> None:
    context = PipelineContext(
        video_path=tmp_path / "sample.mp4",
        out_root=tmp_path / "output" / "sample",
    )
    context.video_path.write_bytes(b"video")
    context.save_json(
        context.stage_dir("02_scenes") / "scenes.json",
        {
            "scenes": [
                {
                    "scene_id": 7,
                    "start_sec": 10.0,
                    "end_sec": 40.0,
                    "duration_sec": 30.0,
                }
            ]
        },
    )

    def fake_run(command, *, capture_output, check):
        Path(command[-1]).write_bytes(b"image")

    monkeypatch.setattr(s03_keyframes.subprocess, "run", fake_run)

    s03_keyframes.run(context)

    payload = context.load_json(
        context.out_root / "03_keyframes" / "keyframes.json"
    )
    assert payload["keyframes"][0] == {
        "scene_id": 7,
        "keyframe_index": 1,
        "keyframe_count": 1,
        "timestamp_sec": 25.0,
        "path": "03_keyframes/frames/scene_007.jpg",
        "size_bytes": 5,
    }
