"""Unit tests for the current local PipelineContext."""

from pathlib import Path

from pipeline.context import PipelineContext


def test_json_round_trip_preserves_korean_text(tmp_path: Path) -> None:
    context = PipelineContext(
        video_path=tmp_path / "sample.mp4",
        out_root=tmp_path / "output" / "sample",
    )
    path = context.stage_dir("test") / "data.json"
    data = {"text": "음성 구간 검출", "segments": [1, 2, 3]}

    context.save_json(path, data)

    assert context.load_json(path) == data
    assert "음성 구간 검출" in path.read_text(encoding="utf-8")


def test_log_dir_is_created_lazily(tmp_path: Path) -> None:
    context = PipelineContext(
        video_path=tmp_path / "sample.mp4",
        out_root=tmp_path / "output" / "sample",
    )

    assert not (context.out_root / "logs").exists()
    assert context.log_dir == context.out_root / "logs"
    assert context.log_dir.is_dir()

