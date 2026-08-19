"""Tests for adaptive keyframe selection and perceptual deduplication."""

import json
from pathlib import Path

import pytest
from PIL import Image

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
    hashes = iter(
        (
            "8000000000000000",
            "0000000000000000",
            "ffffffffffffffff",
            "aaaaaaaaaaaaaaaa",
            "5555555555555555",
            "ff00ff00ff00ff00",
        )
    )
    monkeypatch.setattr(
        s03_keyframes,
        "_perceptual_hash",
        lambda path: next(hashes),
    )

    metrics = s03_keyframes.run(context)

    payload = json.loads(
        (context.out_root / "03_keyframes" / "keyframes.json").read_text(
            encoding="utf-8"
        )
    )
    assert metrics == {
        "candidate_keyframe_count": 6,
        "keyframe_count": 6,
        "removed_keyframe_count": 0,
        "scene_count": 3,
    }
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
    assert payload["deduplication"] == {
        "algorithm": "phash-64-dct-v1",
        "hash_bits": 64,
        "hamming_distance_threshold": 6,
        "comparison_scope": "within_scene",
        "comparison_order": "timestamp_ascending_against_retained",
        "minimum_retained_per_scene": 1,
        "candidate_count": 6,
        "retained_count": 6,
        "removed_count": 0,
        "scene_statistics": [
            {
                "scene_id": 1,
                "candidate_count": 1,
                "retained_count": 1,
                "removed_count": 0,
            },
            {
                "scene_id": 2,
                "candidate_count": 2,
                "retained_count": 2,
                "removed_count": 0,
            },
            {
                "scene_id": 3,
                "candidate_count": 3,
                "retained_count": 3,
                "removed_count": 0,
            },
        ],
        "removed": [],
    }


def test_single_keyframe_keeps_legacy_midpoint_filename(
    tmp_path: Path,
    monkeypatch,
) -> None:
    context = PipelineContext(
        video_path=tmp_path / "sample.mp4",
        out_root=tmp_path / "output" / "sample",
    )
    context.video_path.write_bytes(b"video")
    frames_dir = context.stage_dir("03_keyframes") / "frames"
    frames_dir.mkdir()
    (frames_dir / "scene_007_01.jpg").write_bytes(b"stale")
    (frames_dir / "scene_999.jpg").write_bytes(b"stale")
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
    monkeypatch.setattr(
        s03_keyframes,
        "_perceptual_hash",
        lambda path: "8000000000000000",
    )

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
        "perceptual_hash": "8000000000000000",
    }
    assert [path.name for path in frames_dir.iterdir()] == ["scene_007.jpg"]


def test_deduplication_fixture_preserves_first_and_threshold_boundary(
    tmp_path: Path,
) -> None:
    fixture_path = (
        Path(__file__).parent / "fixtures" / "keyframe_deduplication.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

    retained_pairs = []
    removed_records = []
    for scene in fixture["scenes"]:
        candidates = [
            s03_keyframes._CandidateFrame(
                scene_id=scene["scene_id"],
                candidate_index=item["candidate_index"],
                candidate_count=len(scene["candidates"]),
                timestamp_sec=float(item["timestamp_sec"]),
                path=tmp_path / item["path"],
                perceptual_hash=item["perceptual_hash"],
            )
            for item in scene["candidates"]
        ]
        retained, removed = s03_keyframes._deduplicate_candidates(candidates)
        retained_pairs.extend(
            (candidate.scene_id, candidate.candidate_index)
            for candidate in retained
        )
        removed_records.extend(
            {
                "scene_id": removal.candidate.scene_id,
                "candidate_index": removal.candidate.candidate_index,
                "duplicate_of_candidate_index": (
                    removal.duplicate_of.candidate_index
                ),
                "hamming_distance": removal.hamming_distance,
            }
            for removal in removed
        )

    assert retained_pairs == [tuple(pair) for pair in fixture["retained"]]
    assert removed_records == fixture["removed"]


def test_perceptual_hash_is_stable_and_distinguishes_structure(
    tmp_path: Path,
) -> None:
    vertical = Image.new("L", (64, 64))
    vertical.putdata(
        [0 if x < 32 else 255 for _y in range(64) for x in range(64)]
    )
    horizontal = Image.new("L", (64, 64))
    horizontal.putdata(
        [0 if y < 32 else 255 for y in range(64) for _x in range(64)]
    )
    vertical_path = tmp_path / "vertical.png"
    vertical_copy_path = tmp_path / "vertical-copy.png"
    horizontal_path = tmp_path / "horizontal.png"
    vertical.save(vertical_path)
    vertical.save(vertical_copy_path)
    horizontal.save(horizontal_path)

    vertical_hash = s03_keyframes._perceptual_hash(vertical_path)

    assert len(vertical_hash) == 16
    assert vertical_hash == s03_keyframes._perceptual_hash(vertical_copy_path)
    assert s03_keyframes._hamming_distance(
        vertical_hash,
        s03_keyframes._perceptual_hash(horizontal_path),
    ) > s03_keyframes.HAMMING_DISTANCE_THRESHOLD


def test_stage_removes_duplicate_candidate_and_reindexes_retained_files(
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
                    "scene_id": 4,
                    "start_sec": 0.0,
                    "end_sec": 30.0,
                    "duration_sec": 30.0,
                }
            ]
        },
    )
    commands = []

    def fake_run(command, *, capture_output, check):
        commands.append(command)
        Path(command[-1]).write_bytes(f"image-{len(commands)}".encode())

    hashes = iter(
        (
            "0000000000000000",
            "0000000000000003",
            "ffffffffffffffff",
        )
    )
    monkeypatch.setattr(s03_keyframes.subprocess, "run", fake_run)
    monkeypatch.setattr(
        s03_keyframes,
        "_perceptual_hash",
        lambda path: next(hashes),
    )

    metrics = s03_keyframes.run(context)
    payload = context.load_json(
        context.out_root / "03_keyframes" / "keyframes.json"
    )

    assert metrics == {
        "candidate_keyframe_count": 3,
        "keyframe_count": 2,
        "removed_keyframe_count": 1,
        "scene_count": 1,
    }
    assert [entry["timestamp_sec"] for entry in payload["keyframes"]] == [
        7.5,
        22.5,
    ]
    assert [entry["path"] for entry in payload["keyframes"]] == [
        "03_keyframes/frames/scene_004_01.jpg",
        "03_keyframes/frames/scene_004_02.jpg",
    ]
    assert payload["deduplication"]["scene_statistics"] == [
        {
            "scene_id": 4,
            "candidate_count": 3,
            "retained_count": 2,
            "removed_count": 1,
        }
    ]
    assert payload["deduplication"]["removed"] == [
        {
            "scene_id": 4,
            "candidate_index": 2,
            "candidate_count": 3,
            "timestamp_sec": 15.0,
            "perceptual_hash": "0000000000000003",
            "duplicate_of_keyframe_index": 1,
            "duplicate_of_timestamp_sec": 7.5,
            "duplicate_of_path": (
                "03_keyframes/frames/scene_004_01.jpg"
            ),
            "hamming_distance": 2,
            "reason": "perceptual_hash_distance_lte_threshold",
        }
    ]
    frames = context.out_root / "03_keyframes" / "frames"
    assert sorted(path.name for path in frames.iterdir()) == [
        "scene_004_01.jpg",
        "scene_004_02.jpg",
    ]
