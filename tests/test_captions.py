"""Compatibility tests for the provider-backed caption Stage."""

import json
from pathlib import Path

from pipeline.context import PipelineContext
from pipeline.stages import s08_captions
from video_preprocess.domain import EffectiveModel
from video_preprocess.inference import CaptionBatch
from video_preprocess.storage import LocalArtifactStore, LegacyOutputAdapter


class FakeCaptionService:
    def __init__(self) -> None:
        self.images = []

    def caption(self, images, **kwargs) -> CaptionBatch:
        self.images = list(images)
        assert kwargs["max_new_tokens"] == 40
        return CaptionBatch(
            captions=("first caption", "second caption"),
            model=EffectiveModel(
                provider="fake.caption",
                name="fake/model",
                revision="rev-1",
                runtime="fake/1.0",
            ),
            usage={"input_count": 2},
            timing={"inference_sec": 0.01},
        )


def test_caption_stage_keeps_legacy_output_with_provider_metadata(
    tmp_path: Path,
) -> None:
    context = PipelineContext(
        video_path=tmp_path / "sample.mp4",
        out_root=tmp_path / "output" / "sample",
        caption_model="fake/model",
    )
    frames_dir = context.stage_dir("03_keyframes") / "frames"
    frames_dir.mkdir()
    (frames_dir / "scene_001.jpg").write_bytes(b"first")
    (frames_dir / "scene_002.jpg").write_bytes(b"second")
    keyframes = {
        "keyframes": [
            {
                "scene_id": 1,
                "timestamp_sec": 1.5,
                "path": "03_keyframes/frames/scene_001.jpg",
                "size_bytes": 5,
            },
            {
                "scene_id": 2,
                "timestamp_sec": 4.5,
                "path": "03_keyframes/frames/scene_002.jpg",
                "size_bytes": 6,
            },
        ]
    }
    (context.out_root / "03_keyframes" / "keyframes.json").write_text(
        json.dumps(keyframes),
        encoding="utf-8",
    )
    service = FakeCaptionService()
    store = LocalArtifactStore(context.out_root, namespace="sample")
    context.caption_service = service
    context.artifact_registrar = LegacyOutputAdapter(store)

    result = s08_captions.run(context)

    output = json.loads(
        (context.out_root / "08_captions" / "captions.json").read_text(
            encoding="utf-8"
        )
    )
    assert result == {"caption_count": 2}
    assert len(service.images) == 2
    assert all(
        image.uri.startswith("artifact://sample/")
        for image in service.images
    )
    assert [item["caption"] for item in output["captions"]] == [
        "first caption",
        "second caption",
    ]
    assert output["model"] == "fake/model"
    assert output["provider"] == "fake.caption"
    assert output["revision"] == "rev-1"
    assert output["runtime"] == "fake/1.0"
