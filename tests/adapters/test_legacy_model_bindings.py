"""Contract tests for provider-backed legacy Stage bindings 05-08."""

import asyncio
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline.context import PipelineContext
from video_preprocess.adapters import (
    LegacyStageContractError,
    create_legacy_model_bindings,
)
from video_preprocess.domain import ModelExecution, StageStatus, StageTask
from video_preprocess.engine import DAGPlanner, PipelineEngine, create_default_registry
from video_preprocess.executors import LocalExecutor
from video_preprocess.storage import LocalArtifactStore, LegacyOutputAdapter


MODEL_DATA = {
    "05_vad": ("vad", "local.vad", "silero-vad-v6", "vad-rev"),
    "06_stt": ("stt", "local.stt", "base", "stt-rev"),
    "07_diarize": (
        "diarization",
        "local.diarization",
        "pyannote/model",
        "diarization-rev",
    ),
    "08_captions": (
        "caption",
        "local.caption",
        "caption/model",
        "caption-rev",
    ),
}


def task(
    stage: str,
    version: str,
    inputs,
    config,
    model_bindings,
) -> StageTask:
    return StageTask(
        run_id="run-123",
        stage_run_id=f"stage-{stage}",
        attempt=1,
        stage=stage,
        stage_version=version,
        inputs=inputs,
        config=config,
        model_bindings=model_bindings,
        idempotency_key=f"idem-{stage}",
        trace_id="trace-123",
    )


def fake_modules(restored: list[bool]):
    def vad(ctx):
        write_model_json(
            ctx,
            "05_vad/vad_segments.json",
            "05_vad",
            has_audio=True,
            segments=[{"segment_id": 1}],
        )
        return {"segment_count": 1}

    def stt(ctx):
        write_model_json(
            ctx,
            "06_stt/transcript.json",
            "06_stt",
            segments=[{"text": "hello"}],
        )
        return {"transcript_count": 1}

    def diarize(ctx):
        write_model_json(
            ctx,
            "07_diarize/diarization.json",
            "07_diarize",
            available=True,
            turns=[{"speaker": "SPEAKER_00"}],
        )
        return {"speaker_count": 1}

    def captions(ctx):
        first = ctx.out_root / "03_keyframes" / "frames" / "scene_001.jpg"
        second = ctx.out_root / "03_keyframes" / "frames" / "scene_002.jpg"
        restored.append(
            first.read_bytes() == b"first" and second.read_bytes() == b"second"
        )
        write_model_json(
            ctx,
            "08_captions/captions.json",
            "08_captions",
            captions=[{"scene_id": 1, "caption": "frame"}],
        )
        return {"caption_count": 1}

    return {
        "05_vad": SimpleNamespace(NAME="05_vad", run=vad),
        "06_stt": SimpleNamespace(NAME="06_stt", run=stt),
        "07_diarize": SimpleNamespace(NAME="07_diarize", run=diarize),
        "08_captions": SimpleNamespace(NAME="08_captions", run=captions),
    }


def write_model_json(ctx, relative_path, stage, **payload):
    _, provider, model, revision = MODEL_DATA[stage]
    data = {
        "provider": provider,
        "model": model,
        "revision": revision,
        "runtime": "runtime/1.0",
        **payload,
    }
    path = ctx.out_root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    ctx.save_json(path, data)


def register(registrar, relative_path, artifact_id, kind, media_type):
    return registrar.register_file(
        relative_path,
        artifact_id=artifact_id,
        kind=kind,
        media_type=media_type,
    )


def setup_runtime(tmp_path: Path, modules=None):
    output = tmp_path / "output"
    context = PipelineContext(
        video_path=tmp_path / "video.mp4",
        out_root=output,
    )
    store = LocalArtifactStore(output, namespace="run-123")
    registrar = LegacyOutputAdapter(store)
    restored = []
    bindings = create_legacy_model_bindings(
        context,
        registrar,
        stage_modules=(fake_modules(restored) if modules is None else modules),
    )

    audio_dir = context.stage_dir("04_audio")
    (audio_dir / "audio_16k.wav").write_bytes(b"RIFF-audio")
    context.save_json(
        audio_dir / "audio.json",
        {"has_audio": True, "path": "04_audio/audio_16k.wav"},
    )
    audio = register(
        registrar,
        "04_audio/audio_16k.wav",
        "audio",
        "audio",
        "audio/wav",
    )
    audio_metadata = register(
        registrar,
        "04_audio/audio.json",
        "audio-metadata",
        "json",
        "application/json",
    )

    keyframe_dir = context.stage_dir("03_keyframes")
    context.save_json(
        keyframe_dir / "keyframes.json",
        {
            "keyframes": [
                {
                    "scene_id": 1,
                    "path": "03_keyframes/frames/scene_001.jpg",
                },
                {
                    "scene_id": 2,
                    "path": "03_keyframes/frames/scene_002.jpg",
                },
            ]
        },
    )
    bundle = keyframe_dir / "keyframe_images.zip"
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("03_keyframes/frames/scene_001.jpg", b"first")
        archive.writestr("03_keyframes/frames/scene_002.jpg", b"second")
    keyframes = register(
        registrar,
        "03_keyframes/keyframes.json",
        "keyframes",
        "json",
        "application/json",
    )
    keyframe_images = register(
        registrar,
        "03_keyframes/keyframe_images.zip",
        "keyframe-images",
        "archive",
        "application/zip",
    )
    return (
        context,
        store,
        registrar,
        bindings,
        restored,
        audio,
        audio_metadata,
        keyframes,
        keyframe_images,
    )


def test_model_bindings_publish_effective_models_and_restore_keyframes(
    tmp_path: Path,
) -> None:
    (
        context,
        store,
        _,
        bindings,
        restored,
        audio,
        audio_metadata,
        keyframes,
        keyframe_images,
    ) = setup_runtime(tmp_path)

    async def scenario():
        executor = LocalExecutor(bindings)
        vad_task = task(
            "05_vad",
            "1.0.0",
            {"audio": audio, "audio_metadata": audio_metadata},
            {"vad_min_silence_ms": 450, "vad_speech_pad_ms": 180},
            {"vad": "vad.default"},
        )
        vad = await executor.result(await executor.submit(vad_task))
        stt_task = task(
            "06_stt",
            "1.0.0",
            {"audio": audio, "vad_segments": vad.outputs["vad_segments"]},
            {
                "stt_merge_gap_sec": 0.4,
                "language": "ko",
                "whisper_model": "base",
            },
            {"stt": "stt.default"},
        )
        stt = await executor.result(await executor.submit(stt_task))
        diarization_task = task(
            "07_diarize",
            "1.0.0",
            {"audio": audio},
            {"diarize_model": "pyannote/model"},
            {"diarization": "diarization.default"},
        )
        diarization = await executor.result(
            await executor.submit(diarization_task)
        )
        caption_task = task(
            "08_captions",
            "1.3.0",
            {
                "keyframes": keyframes,
                "keyframe_images": keyframe_images,
            },
            {"caption_model": "caption/model"},
            {"caption": "caption.default"},
        )
        captions = await executor.result(await executor.submit(caption_task))
        return vad, stt, diarization, captions

    results = asyncio.run(scenario())

    assert bindings.names == (
        "05_vad",
        "06_stt",
        "07_diarize",
        "08_captions",
    )
    assert all(result.status is StageStatus.SUCCEEDED for result in results)
    for result, stage in zip(results, MODEL_DATA):
        slot, provider, model, revision = MODEL_DATA[stage]
        assert result.models == (
            ModelExecution(
                slot=slot,
                provider=provider,
                model=model,
                revision=revision,
                runtime="runtime/1.0",
            ),
        )
        assert all(store.verify(ref).ok for ref in result.outputs.values())
    assert restored == [True]
    assert context.vad_min_silence_ms == 500
    assert context.language is None
    assert context.caption_model == "Salesforce/blip-image-captioning-base"


def test_pipeline_engine_exact_plans_run_model_bindings(tmp_path: Path) -> None:
    runtime = setup_runtime(tmp_path)
    _, _, _, bindings, _, audio, audio_metadata, keyframes, images = runtime
    planner = DAGPlanner(create_default_registry())

    async def scenario():
        engine = PipelineEngine(LocalExecutor(bindings))
        vad = await engine.run(
            planner.plan(stage="05_vad"),
            run_id="run-123",
            trace_id="trace-vad",
            artifacts={"audio": audio, "audio_metadata": audio_metadata},
            stage_configs={
                "05_vad": {
                    "vad_min_silence_ms": 500,
                    "vad_speech_pad_ms": 200,
                }
            },
            model_bindings={"05_vad": {"vad": "vad.default"}},
        )
        stt = await engine.run(
            planner.plan(stage="06_stt"),
            run_id="run-123",
            trace_id="trace-stt",
            artifacts={
                "audio": audio,
                "vad_segments": vad.artifacts["vad_segments"],
            },
            stage_configs={
                "06_stt": {
                    "stt_merge_gap_sec": 0.5,
                    "language": None,
                    "whisper_model": "base",
                }
            },
            model_bindings={"06_stt": {"stt": "stt.default"}},
        )
        diarization = await engine.run(
            planner.plan(stage="07_diarize"),
            run_id="run-123",
            trace_id="trace-diarization",
            artifacts={"audio": audio},
            stage_configs={
                "07_diarize": {"diarize_model": "pyannote/model"}
            },
            model_bindings={
                "07_diarize": {
                    "diarization": "diarization.default"
                }
            },
        )
        captions = await engine.run(
            planner.plan(stage="08_captions"),
            run_id="run-123",
            trace_id="trace-captions",
            artifacts={"keyframes": keyframes, "keyframe_images": images},
            stage_configs={
                "08_captions": {"caption_model": "caption/model"}
            },
            model_bindings={
                "08_captions": {"caption": "caption.default"}
            },
        )
        return vad, stt, diarization, captions

    results = asyncio.run(scenario())

    assert all(result.status.value == "succeeded" for result in results)
    assert [result.stages[0].result.models[0].slot for result in results] == [
        "vad",
        "stt",
        "diarization",
        "caption",
    ]


def test_no_speech_and_optional_diarization_are_explicit_skips(
    tmp_path: Path,
) -> None:
    def vad(ctx):
        write_model_json(
            ctx,
            "05_vad/vad_segments.json",
            "05_vad",
            has_audio=True,
            segments=[],
        )
        return {"segment_count": 0}

    def stt(ctx):
        ctx.save_json(
            ctx.stage_dir("06_stt") / "transcript.json",
            {"segments": [], "language": None},
        )
        return {"transcript_count": 0}

    def diarize(ctx):
        ctx.save_json(
            ctx.stage_dir("07_diarize") / "diarization.json",
            {
                "available": False,
                "reason": "환경변수/.env에 HF_TOKEN 없음",
                "turns": [],
            },
        )
        return {"speaker_count": 0, "skipped": "credential"}

    modules = fake_modules([])
    modules.update(
        {
            "05_vad": SimpleNamespace(NAME="05_vad", run=vad),
            "06_stt": SimpleNamespace(NAME="06_stt", run=stt),
            "07_diarize": SimpleNamespace(NAME="07_diarize", run=diarize),
        }
    )
    runtime = setup_runtime(tmp_path, modules)
    _, _, _, bindings, _, audio, audio_metadata, _, _ = runtime
    vad_result = bindings.get("05_vad")(
        task(
            "05_vad",
            "1.0.0",
            {"audio": audio, "audio_metadata": audio_metadata},
            {"vad_min_silence_ms": 500, "vad_speech_pad_ms": 200},
            {"vad": "vad.default"},
        )
    )
    stt_result = bindings.get("06_stt")(
        task(
            "06_stt",
            "1.0.0",
            {"audio": audio, "vad_segments": vad_result.outputs["vad_segments"]},
            {
                "stt_merge_gap_sec": 0.5,
                "language": None,
                "whisper_model": "base",
            },
            {"stt": "stt.default"},
        )
    )
    diarization = bindings.get("07_diarize")(
        task(
            "07_diarize",
            "1.0.0",
            {"audio": audio},
            {"diarize_model": "pyannote/model"},
            {"diarization": "diarization.default"},
        )
    )

    assert stt_result.status is StageStatus.SKIPPED
    assert stt_result.reason_code == "NO_SPEECH"
    assert stt_result.outputs["transcript"]
    assert diarization.status is StageStatus.SKIPPED
    assert diarization.reason_code == "OPTIONAL_DIARIZATION_UNAVAILABLE"
    assert diarization.outputs["diarization"]


def test_model_binding_alias_is_exact_and_effective_metadata_is_required(
    tmp_path: Path,
) -> None:
    runtime = setup_runtime(tmp_path)
    context, _, _, bindings, _, audio, audio_metadata, _, _ = runtime
    invalid_task = task(
        "05_vad",
        "1.0.0",
        {"audio": audio, "audio_metadata": audio_metadata},
        {"vad_min_silence_ms": 500, "vad_speech_pad_ms": 200},
        {"vad": "vad.remote"},
    )

    with pytest.raises(LegacyStageContractError, match="model bindings"):
        bindings.get("05_vad")(invalid_task)

    def missing_model(ctx):
        ctx.save_json(
            ctx.stage_dir("05_vad") / "vad_segments.json",
            {"has_audio": True, "segments": [{"segment_id": 1}]},
        )
        return {"segment_count": 1}

    modules = fake_modules([])
    modules["05_vad"] = SimpleNamespace(NAME="05_vad", run=missing_model)
    broken = create_legacy_model_bindings(
        context,
        context.artifact_registrar,
        stage_modules=modules,
    ).get("05_vad")

    with pytest.raises(LegacyStageContractError, match="effective model"):
        broken(
            task(
                "05_vad",
                "1.0.0",
                {"audio": audio, "audio_metadata": audio_metadata},
                {"vad_min_silence_ms": 500, "vad_speech_pad_ms": 200},
                {"vad": "vad.default"},
            )
        )


def test_caption_rejects_bundle_with_unlisted_member(tmp_path: Path) -> None:
    runtime = setup_runtime(tmp_path)
    context, _, registrar, bindings, _, _, _, keyframes, _ = runtime
    bundle = context.out_root / "03_keyframes" / "keyframe_images.zip"
    with zipfile.ZipFile(bundle, "a") as archive:
        archive.writestr("03_keyframes/frames/unlisted.jpg", b"extra")
    changed_bundle = register(
        registrar,
        "03_keyframes/keyframe_images.zip",
        "changed-bundle",
        "archive",
        "application/zip",
    )

    with pytest.raises(LegacyStageContractError, match="members"):
        bindings.get("08_captions")(
            task(
                "08_captions",
                "1.3.0",
                {
                    "keyframes": keyframes,
                    "keyframe_images": changed_bundle,
                },
                {"caption_model": "caption/model"},
                {"caption": "caption.default"},
            )
        )
