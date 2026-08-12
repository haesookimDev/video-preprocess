"""Contract tests for final legacy bindings and full DAG composition."""

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline.context import PipelineContext
from video_preprocess.adapters import (
    LegacyStageContractError,
    create_legacy_final_bindings,
    create_legacy_pipeline_bindings,
)
from video_preprocess.domain import ArtifactRef, Checksum, StageTask
from video_preprocess.engine import (
    DAGPlanner,
    PipelineEngine,
    create_default_registry,
)
from video_preprocess.executors import LocalExecutor
from video_preprocess.storage import LocalArtifactStore, LegacyOutputAdapter


MODEL_METADATA = {
    "05_vad": ("local.vad", "silero-vad-v6", "vad-rev"),
    "06_stt": ("local.stt", "base", "stt-rev"),
    "07_diarize": (
        "local.diarization",
        "pyannote/model",
        "diarization-rev",
    ),
    "08_captions": ("local.caption", "caption/model", "caption-rev"),
}


def external_video(path: Path) -> ArtifactRef:
    payload = path.read_bytes()
    return ArtifactRef(
        artifact_id="video",
        kind="video",
        uri="artifact://external/video",
        media_type="video/mp4",
        size_bytes=len(payload),
        checksum=Checksum("sha256", hashlib.sha256(payload).hexdigest()),
    )


def write_model_output(ctx, stage, filename, **values):
    provider, model, revision = MODEL_METADATA[stage]
    ctx.save_json(
        ctx.stage_dir(stage) / filename,
        {
            "provider": provider,
            "model": model,
            "revision": revision,
            "runtime": "runtime/1.0",
            **values,
        },
    )


def full_fake_modules():
    def probe(ctx):
        ctx.save_json(
            ctx.stage_dir("01_probe") / "metadata.json",
            {"summary": {"duration_sec": 8.0, "size_bytes": 10}},
        )
        return {"duration_sec": 8.0}

    def scenes(ctx):
        ctx.save_json(
            ctx.stage_dir("02_scenes") / "scenes.json",
            {
                "scenes": [
                    {
                        "scene_id": 1,
                        "start_sec": 0.0,
                        "end_sec": 8.0,
                        "duration_sec": 8.0,
                    }
                ]
            },
        )
        (ctx.stage_dir("02_scenes") / "scene_stats.csv").write_text(
            "frame,content_val\n1,2.0\n",
            encoding="utf-8",
        )
        return {"scene_count": 1}

    def keyframes(ctx):
        frame = ctx.stage_dir("03_keyframes") / "frames" / "scene_001.jpg"
        frame.parent.mkdir(parents=True, exist_ok=True)
        frame.write_bytes(b"image")
        ctx.save_json(
            ctx.stage_dir("03_keyframes") / "keyframes.json",
            {
                "keyframes": [
                    {
                        "scene_id": 1,
                        "path": "03_keyframes/frames/scene_001.jpg",
                    }
                ]
            },
        )
        return {"keyframe_count": 1}

    def audio(ctx):
        (ctx.stage_dir("04_audio") / "audio_16k.wav").write_bytes(b"RIFF")
        ctx.save_json(
            ctx.stage_dir("04_audio") / "audio.json",
            {"has_audio": True, "path": "04_audio/audio_16k.wav"},
        )
        return {"has_audio": True}

    def vad(ctx):
        write_model_output(
            ctx,
            "05_vad",
            "vad_segments.json",
            has_audio=True,
            segments=[{"segment_id": 1, "start_sec": 0.0, "end_sec": 2.0}],
        )
        return {"segment_count": 1}

    def stt(ctx):
        write_model_output(
            ctx,
            "06_stt",
            "transcript.json",
            segments=[
                {"start_sec": 0.0, "end_sec": 2.0, "text": "hello"}
            ],
        )
        return {"transcript_count": 1}

    def diarize(ctx):
        write_model_output(
            ctx,
            "07_diarize",
            "diarization.json",
            available=True,
            speakers=["SPEAKER_00"],
            turns=[],
        )
        return {"speaker_count": 1}

    def captions(ctx):
        restored = ctx.out_root / "03_keyframes" / "frames" / "scene_001.jpg"
        assert restored.read_bytes() == b"image"
        write_model_output(
            ctx,
            "08_captions",
            "captions.json",
            captions=[{"scene_id": 1, "caption": "a frame"}],
        )
        return {"caption_count": 1}

    def timeline(ctx):
        ctx.save_json(
            ctx.stage_dir("09_timeline") / "timeline.json",
            {
                "scene_cards": [
                    {
                        "scene_id": 1,
                        "start_sec": 0.0,
                        "end_sec": 8.0,
                        "caption": "a frame",
                        "transcript": [],
                    }
                ]
            },
        )
        (ctx.stage_dir("09_timeline") / "timeline.md").write_text(
            "# Timeline\n",
            encoding="utf-8",
        )
        return {"scene_card_count": 1}

    def index(ctx):
        (ctx.stage_dir("10_index") / "index.db").write_bytes(b"SQLite")
        ctx.save_json(
            ctx.stage_dir("10_index") / "index_summary.json",
            {
                "embed_provider": "local.embedding",
                "embed_model": ctx.embed_model,
                "embed_revision": "embedding-rev",
                "embed_runtime": "runtime/1.0",
                "embed_dim": 3,
                "card_count": 1,
            },
        )
        return {"card_count": 1, "embed_dim": 3}

    def context(ctx):
        (ctx.stage_dir("11_context") / "context.md").write_text(
            "# Context\n",
            encoding="utf-8",
        )
        ctx.save_json(
            ctx.stage_dir("11_context") / "context.json",
            {"scene_cards": [], "stats": {"chars": 10}},
        )
        return {"chars": 10}

    functions = (
        probe,
        scenes,
        keyframes,
        audio,
        vad,
        stt,
        diarize,
        captions,
        timeline,
        index,
        context,
    )
    names = (
        "01_probe",
        "02_scenes",
        "03_keyframes",
        "04_audio",
        "05_vad",
        "06_stt",
        "07_diarize",
        "08_captions",
        "09_timeline",
        "10_index",
        "11_context",
    )
    return {
        name: SimpleNamespace(NAME=name, run=function)
        for name, function in zip(names, functions)
    }


def pipeline_options():
    return {
        "stage_configs": {
            "02_scenes": {
                "scene_threshold": 27.0,
                "min_scene_len_frames": 15,
            },
            "03_keyframes": {"keyframes_per_scene": 1},
            "05_vad": {
                "vad_min_silence_ms": 500,
                "vad_speech_pad_ms": 200,
            },
            "06_stt": {
                "stt_merge_gap_sec": 0.5,
                "language": None,
                "whisper_model": "base",
            },
            "07_diarize": {"diarize_model": "pyannote/model"},
            "08_captions": {"caption_model": "caption/model"},
            "10_index": {"embed_model": "embedding/model"},
        },
        "model_bindings": {
            "05_vad": {"vad": "vad.default"},
            "06_stt": {"stt": "stt.default"},
            "07_diarize": {"diarization": "diarization.default"},
            "08_captions": {"caption": "caption.default"},
            "10_index": {"embedding": "embedding.default"},
        },
    }


def test_full_default_dag_runs_through_one_legacy_binding_registry(
    tmp_path: Path,
) -> None:
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake-video")
    output = tmp_path / "output"
    context = PipelineContext(video_path=video, out_root=output)
    store = LocalArtifactStore(output, namespace="run-123")
    bindings = create_legacy_pipeline_bindings(
        context,
        LegacyOutputAdapter(store),
        stage_modules=full_fake_modules(),
    )
    engine = PipelineEngine(LocalExecutor(bindings))

    result = asyncio.run(
        engine.run(
            DAGPlanner(create_default_registry()).plan(),
            run_id="run-123",
            trace_id="trace-123",
            artifacts={"video": external_video(video)},
            **pipeline_options(),
        )
    )

    assert result.status.value == "succeeded"
    assert bindings.names == tuple(full_fake_modules())
    assert [record.stage for record in result.stages] == list(bindings.names)
    assert all(
        record.result.status.value == "succeeded"
        for record in result.stages
    )
    assert set(result.artifacts) >= {
        "timeline",
        "timeline_markdown",
        "search_index",
        "index_summary",
        "context",
        "context_json",
    }
    generated = (
        ref for name, ref in result.artifacts.items() if name != "video"
    )
    assert all(store.verify(ref).ok for ref in generated)
    assert result.stages[9].result.models[0].slot == "embedding"
    assert result.stages[9].result.models[0].model == "embedding/model"
    assert context.embed_model == "paraphrase-multilingual-MiniLM-L12-v2"


def final_task(stage, inputs, *, config=None, model_bindings=None):
    stage_version = create_default_registry().get(stage).stage_version
    return StageTask(
        run_id="run-123",
        stage_run_id=f"stage-{stage}",
        attempt=1,
        stage=stage,
        stage_version=stage_version,
        inputs=inputs,
        config={} if config is None else config,
        model_bindings={} if model_bindings is None else model_bindings,
        idempotency_key=f"idem-{stage}",
        trace_id="trace-123",
    )


def test_index_requires_effective_embedding_metadata(tmp_path: Path) -> None:
    output = tmp_path / "output"
    context = PipelineContext(video_path=tmp_path / "video.mp4", out_root=output)
    store = LocalArtifactStore(output, namespace="run-123")
    registrar = LegacyOutputAdapter(store)
    context.save_json(
        context.stage_dir("09_timeline") / "timeline.json",
        {"scene_cards": []},
    )
    timeline = registrar.register_json(
        "09_timeline/timeline.json",
        artifact_id="timeline",
    )
    modules = full_fake_modules()

    def broken_index(ctx):
        (ctx.stage_dir("10_index") / "index.db").write_bytes(b"SQLite")
        ctx.save_json(
            ctx.stage_dir("10_index") / "index_summary.json",
            {"embed_model": ctx.embed_model},
        )
        return {"card_count": 0}

    modules["10_index"] = SimpleNamespace(NAME="10_index", run=broken_index)
    final_modules = {
        name: modules[name]
        for name in ("09_timeline", "10_index", "11_context")
    }
    runner = create_legacy_final_bindings(
        context,
        registrar,
        stage_modules=final_modules,
    ).get("10_index")

    with pytest.raises(LegacyStageContractError, match="effective model"):
        runner(
            final_task(
                "10_index",
                {"timeline": timeline},
                config={"embed_model": "embedding/model"},
                model_bindings={"embedding": "embedding.default"},
            )
        )


def test_timeline_requires_json_and_markdown_outputs(tmp_path: Path) -> None:
    output = tmp_path / "output"
    context = PipelineContext(video_path=tmp_path / "video.mp4", out_root=output)
    store = LocalArtifactStore(output, namespace="run-123")
    registrar = LegacyOutputAdapter(store)
    relative_inputs = {
        "scenes": "02_scenes/scenes.json",
        "keyframes": "03_keyframes/keyframes.json",
        "transcript": "06_stt/transcript.json",
        "diarization": "07_diarize/diarization.json",
        "captions": "08_captions/captions.json",
    }
    inputs = {}
    for name, relative in relative_inputs.items():
        path = output / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        context.save_json(path, {})
        inputs[name] = registrar.register_json(relative, artifact_id=name)
    modules = full_fake_modules()

    def missing_markdown(ctx):
        ctx.save_json(
            ctx.stage_dir("09_timeline") / "timeline.json",
            {"scene_cards": []},
        )
        return {"scene_card_count": 0}

    modules["09_timeline"] = SimpleNamespace(
        NAME="09_timeline",
        run=missing_markdown,
    )
    final_modules = {
        name: modules[name]
        for name in ("09_timeline", "10_index", "11_context")
    }
    runner = create_legacy_final_bindings(
        context,
        registrar,
        stage_modules=final_modules,
    ).get("09_timeline")

    with pytest.raises(LegacyStageContractError, match="timeline_markdown"):
        runner(final_task("09_timeline", inputs))
