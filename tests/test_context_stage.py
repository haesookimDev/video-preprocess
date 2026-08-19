"""Tests for actual-token budgeting in the final context Stage."""

import json
from pathlib import Path

from pipeline.context import PipelineContext
from pipeline.stages import s11_context


class CharacterTokenCounter:
    model_name = "fake/tokenizer"

    @staticmethod
    def count(text):
        return len(text)

    @staticmethod
    def truncate(text, max_tokens):
        return text[:max_tokens]


def test_context_stage_never_exceeds_configured_token_budget(
    tmp_path: Path,
) -> None:
    context = PipelineContext(
        video_path=tmp_path / "sample.mp4",
        out_root=tmp_path / "output",
        max_context_tokens=800,
        context_tokenizer_model="fake/tokenizer",
    )
    context.context_token_counter = CharacterTokenCounter()
    context.save_json(
        context.stage_dir("01_probe") / "metadata.json",
        {"summary": {"duration_sec": 30.0, "size_bytes": 1000}},
    )
    context.save_json(
        context.stage_dir("07_diarize") / "diarization.json",
        {"speakers": ["SPEAKER_00"]},
    )
    cards = [
        {
            "scene_id": scene_id,
            "start_sec": float((scene_id - 1) * 10),
            "end_sec": float(scene_id * 10),
            "caption": f"scene {scene_id} " + "설명" * 80,
            "ocr_text": "OPENAI 화면" if scene_id == 1 else None,
            "chapter": {"title": "Opening"} if scene_id == 1 else None,
            "subtitle_text": "Welcome" if scene_id == 1 else None,
            "transcript": [],
        }
        for scene_id in range(1, 5)
    ]
    context.save_json(
        context.stage_dir("09_timeline") / "timeline.json",
        {"scene_cards": cards},
    )

    result = s11_context.run(context)
    payload = json.loads(
        (context.out_root / "11_context" / "context.json").read_text(
            encoding="utf-8"
        )
    )
    markdown = (
        context.out_root / "11_context" / "context.md"
    ).read_text(encoding="utf-8")

    budget = payload["stats"]["token_budget"]
    assert result["token_count"] <= 800
    assert len(markdown) <= 800
    assert budget["tokenizer_model"] == "fake/tokenizer"
    assert budget["included_scene_ids"][0] == 1
    assert budget["excluded_scene_ids"]
    assert set(budget["included_scene_ids"]).isdisjoint(
        budget["excluded_scene_ids"]
    )


def test_context_stage_includes_ocr_text_in_markdown(tmp_path: Path) -> None:
    context = PipelineContext(
        video_path=tmp_path / "sample.mp4",
        out_root=tmp_path / "output",
    )
    context.save_json(
        context.stage_dir("01_probe") / "metadata.json",
        {"summary": {"duration_sec": 10.0, "size_bytes": 1000}},
    )
    context.save_json(
        context.stage_dir("07_diarize") / "diarization.json",
        {"speakers": []},
    )
    context.save_json(
        context.stage_dir("09_timeline") / "timeline.json",
        {
            "scene_cards": [
                {
                    "scene_id": 1,
                    "start_sec": 0.0,
                    "end_sec": 10.0,
                    "caption": "dashboard",
                    "ocr_text": "OPENAI 화면",
                    "chapter": {"title": "Opening"},
                    "subtitle_text": "Welcome subtitle",
                    "audio_event_text": "music",
                    "audio_events": [
                        {"label": "music", "confidence": 0.91}
                    ],
                    "transcript": [],
                }
            ]
        },
    )

    s11_context.run(context)

    markdown = (
        context.out_root / "11_context" / "context.md"
    ).read_text(encoding="utf-8")
    assert "화면 텍스트: OPENAI 화면" in markdown
    assert "챕터: Opening" in markdown
    assert "내장 자막: Welcome subtitle" in markdown
    assert "오디오 이벤트: music (0.91)" in markdown


def test_context_stage_requires_counter_when_budget_is_enabled(
    tmp_path: Path,
) -> None:
    context = PipelineContext(
        video_path=tmp_path / "sample.mp4",
        out_root=tmp_path / "output",
        max_context_tokens=500,
    )
    context.save_json(
        context.stage_dir("01_probe") / "metadata.json",
        {"summary": {"duration_sec": 1.0, "size_bytes": 1}},
    )
    context.save_json(
        context.stage_dir("07_diarize") / "diarization.json",
        {"speakers": []},
    )
    context.save_json(
        context.stage_dir("09_timeline") / "timeline.json",
        {"scene_cards": []},
    )

    try:
        s11_context.run(context)
    except RuntimeError as exc:
        assert str(exc) == "context token counter is not configured"
    else:
        raise AssertionError("missing token counter was accepted")
