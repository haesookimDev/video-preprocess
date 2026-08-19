"""Integration coverage for the selected-scene reprocessing DAG bindings."""

import asyncio
import json
import zipfile
from pathlib import Path

from pipeline.context import PipelineContext
from video_preprocess.adapters import create_legacy_reprocessing_bindings
from video_preprocess.engine import (
    DAGPlanner,
    PipelineEngine,
    create_reprocessing_registry,
)
from video_preprocess.executors import LocalExecutor
from video_preprocess.storage import LocalArtifactStore, LegacyOutputAdapter


COMMON_CONFIG = {
    "reprocessing_source_run_id": "parent-run",
    "reprocessing_profile": "visual-detail-v1",
    "reprocessing_scene_ids": (2,),
    "reprocessing_overlay_policy": "copy-unselected-from-source-v1",
}


class FakeStage:
    def __init__(self, name, callback):
        self.NAME = name
        self.callback = callback

    def run(self, context):
        return self.callback(context)


def _save(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _source_artifacts(root: Path, store: LocalArtifactStore):
    video = root / "00_source" / "00_input" / "video.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    _save(root / "00_source/01_probe/metadata.json", {"summary": {}})
    _save(root / "00_source/02_scenes/scenes.json", {"scenes": []})
    keyframe = "03_keyframes/frames/scene_001.jpg"
    _save(
        root / "00_source/03_keyframes/keyframes.json",
        {"keyframes": [{"path": keyframe}]},
    )
    bundle = root / "00_source/03_keyframes/keyframe_images.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr(keyframe, b"source-frame")
    _save(root / "00_source/04_embedded_text/embedded_text.json", {})
    _save(root / "00_source/05_audio_events/audio_events.json", {})
    _save(root / "00_source/06_stt/transcript.json", {})
    _save(root / "00_source/07_diarize/diarization.json", {})
    _save(root / "00_source/08_captions/captions.json", {"captions": []})
    _save(root / "00_source/08_ocr/ocr.json", {"results": []})
    paths = {
        "video": "00_source/00_input/video.mp4",
        "metadata": "00_source/01_probe/metadata.json",
        "scenes": "00_source/02_scenes/scenes.json",
        "source_keyframes": "00_source/03_keyframes/keyframes.json",
        "source_keyframe_images": (
            "00_source/03_keyframes/keyframe_images.zip"
        ),
        "embedded_text": "00_source/04_embedded_text/embedded_text.json",
        "audio_events": "00_source/05_audio_events/audio_events.json",
        "transcript": "00_source/06_stt/transcript.json",
        "diarization": "00_source/07_diarize/diarization.json",
        "source_captions": "00_source/08_captions/captions.json",
        "source_ocr": "00_source/08_ocr/ocr.json",
    }
    return video, {
        name: store.register_existing(
            relative,
            artifact_id=f"imported:{name}",
            kind="artifact",
            media_type="application/octet-stream",
        )
        for name, relative in paths.items()
    }


def _fake_modules(seen):
    def keyframes(context):
        assert (
            context.out_root
            / "00_source/03_keyframes/frames/scene_001.jpg"
        ).read_bytes() == b"source-frame"
        relative = "03_keyframes/frames/scene_002.jpg"
        frame = context.out_root / relative
        frame.parent.mkdir(parents=True, exist_ok=True)
        frame.write_bytes(b"selected-frame")
        _save(
            context.out_root / "03_keyframes/keyframes.json",
            {"keyframes": [{"path": relative}]},
        )
        seen.append(("03_keyframes", context.reprocessing_scene_ids))
        return {"processed_scene_count": 1}

    def captions(context):
        _save(
            context.out_root / "08_captions/captions.json",
            {
                "provider": "fake.caption",
                "model": "caption/model",
                "revision": "rev-1",
                "runtime": "fake/1",
                "captions": [{"caption": "selected"}],
            },
        )
        seen.append(("08_captions", context.reprocessing_scene_ids))
        return {"caption_count": 1}

    def ocr(context):
        _save(
            context.out_root / "08_ocr/ocr.json",
            {
                "executed": True,
                "provider": "fake.ocr",
                "model": "ocr/model",
                "revision": "rev-1",
                "runtime": "fake/1",
                "results": [],
            },
        )
        seen.append(("08_ocr", context.reprocessing_scene_ids))
        return {"ocr_image_count": 0}

    def timeline(context):
        _save(context.out_root / "09_timeline/timeline.json", {"cards": []})
        (context.out_root / "09_timeline/timeline.md").write_text(
            "# timeline\n",
            encoding="utf-8",
        )
        seen.append(("09_timeline", context.reprocessing_scene_ids))
        return {"scene_card_count": 0}

    def index(context):
        path = context.out_root / "10_index/index.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"sqlite")
        _save(
            context.out_root / "10_index/index_summary.json",
            {
                "embed_provider": "fake.embedding",
                "embed_model": "embedding/model",
                "embed_revision": "rev-1",
                "embed_runtime": "fake/1",
            },
        )
        seen.append(("10_index", ()))
        return {"indexed_scene_count": 0}

    def final_context(context):
        path = context.out_root / "11_context/context.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# context\n", encoding="utf-8")
        _save(context.out_root / "11_context/context.json", {"cards": []})
        seen.append(("11_context", context.reprocessing_scene_ids))
        return {"chars": 10}

    return {
        "03_keyframes": FakeStage("03_keyframes", keyframes),
        "08_captions": FakeStage("08_captions", captions),
        "08_ocr": FakeStage("08_ocr", ocr),
        "09_timeline": FakeStage("09_timeline", timeline),
        "10_index": FakeStage("10_index", index),
        "11_context": FakeStage("11_context", final_context),
    }


def test_reprocessing_registry_runs_imported_boundary_through_six_stages(
    tmp_path: Path,
) -> None:
    output = tmp_path / "derived"
    store = LocalArtifactStore(output, namespace="derived-run")
    video, artifacts = _source_artifacts(output, store)
    context = PipelineContext(video_path=video, out_root=output)
    seen = []
    bindings = create_legacy_reprocessing_bindings(
        context,
        LegacyOutputAdapter(store),
        stage_modules=_fake_modules(seen),
    )
    engine = PipelineEngine(LocalExecutor(bindings, max_concurrency=1))
    stage_configs = {
        "03_keyframes": {"keyframes_per_scene": 3, **COMMON_CONFIG},
        "08_captions": {"caption_model": "caption/model", **COMMON_CONFIG},
        "08_ocr": {
            "ocr_mode": "all",
            "ocr_model": "ocr/model",
            "ocr_languages": ("eng", "kor"),
            "ocr_detect_orientation": True,
            "ocr_min_confidence": 0.5,
            **COMMON_CONFIG,
        },
        "09_timeline": dict(COMMON_CONFIG),
        "10_index": {"embed_model": "embedding/model"},
        "11_context": {
            "max_context_tokens": None,
            "context_tokenizer_model": "tokenizer/model",
            **COMMON_CONFIG,
        },
    }

    result = asyncio.run(
        engine.run(
            DAGPlanner(create_reprocessing_registry()).plan(),
            run_id="derived-run",
            trace_id="trace-reprocessing",
            artifacts=artifacts,
            stage_configs=stage_configs,
            model_bindings={
                "08_captions": {"caption": "caption.default"},
                "08_ocr": {"ocr": "ocr.default"},
                "10_index": {"embedding": "embedding.default"},
            },
        )
    )

    assert result.status.value == "succeeded"
    assert [record.stage for record in result.stages] == [
        "03_keyframes",
        "08_captions",
        "08_ocr",
        "09_timeline",
        "10_index",
        "11_context",
    ]
    assert [name for name, _ in seen] == [
        record.stage for record in result.stages
    ]
    assert context.reprocessing_scene_ids == ()
    assert all(
        store.verify(ref).ok
        for name, ref in result.artifacts.items()
        if name not in artifacts
    )
