"""Contract tests for StageTask bindings over the current ``run(ctx)`` API."""

import asyncio
import hashlib
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline.context import PipelineContext
from video_preprocess.adapters import (
    LegacyStageContractError,
    create_legacy_media_bindings,
)
from video_preprocess.adapters.legacy_stages import (
    LegacyStageDefinition,
    _create_binding_registry,
)
from video_preprocess.domain import ArtifactRef, Checksum, StageStatus, StageTask
from video_preprocess.engine import (
    DAGPlanner,
    DEFAULT_STAGE_SPECS,
    PipelineEngine,
    StageRegistry,
)
from video_preprocess.executors import LocalExecutor
from video_preprocess.storage import LocalArtifactStore, LegacyOutputAdapter


def path_ref(path: Path, name: str) -> ArtifactRef:
    payload = path.read_bytes()
    return ArtifactRef(
        artifact_id=f"art-{name}",
        kind="video" if name == "video" else "json",
        uri=f"artifact://external/{name}",
        media_type=(
            "video/mp4" if name == "video" else "application/json"
        ),
        size_bytes=len(payload),
        checksum=Checksum("sha256", hashlib.sha256(payload).hexdigest()),
    )


def task(
    stage: str,
    version: str,
    inputs: dict[str, ArtifactRef],
    *,
    config: dict[str, object] | None = None,
    model_bindings: dict[str, str] | None = None,
) -> StageTask:
    return StageTask(
        run_id="run-123",
        stage_run_id=f"stage-{stage}",
        attempt=1,
        stage=stage,
        stage_version=version,
        inputs=inputs,
        config={} if config is None else config,
        model_bindings={} if model_bindings is None else model_bindings,
        idempotency_key=f"idem-{stage}",
        trace_id="trace-123",
    )


def fake_modules(seen: list[tuple[str, object]]):
    def probe(ctx):
        seen.append(("01_probe", None))
        ctx.save_json(
            ctx.stage_dir("01_probe") / "metadata.json",
            {"summary": {"audio": {"codec": "aac"}}},
        )
        return {"duration_sec": 10.0}

    def scenes(ctx):
        seen.append(
            (
                "02_scenes",
                (ctx.scene_threshold, ctx.min_scene_len_frames),
            )
        )
        ctx.save_json(
            ctx.stage_dir("02_scenes") / "scenes.json",
            {
                "scenes": [
                    {
                        "scene_id": 1,
                        "start_sec": 0.0,
                        "end_sec": 10.0,
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
        seen.append(("03_keyframes", ctx.keyframes_per_scene))
        frames = ctx.stage_dir("03_keyframes") / "frames"
        frames.mkdir(parents=True, exist_ok=True)
        first = frames / "scene_001.jpg"
        second = frames / "scene_002.jpg"
        first.write_bytes(b"first-image")
        second.write_bytes(b"second-image")
        ctx.save_json(
            ctx.stage_dir("03_keyframes") / "keyframes.json",
            {
                "keyframes": [
                    {
                        "scene_id": 2,
                        "path": "03_keyframes/frames/scene_002.jpg",
                    },
                    {
                        "scene_id": 1,
                        "path": "03_keyframes/frames/scene_001.jpg",
                    },
                ]
            },
        )
        return {"keyframe_count": 2}

    def audio(ctx):
        seen.append(("04_audio", None))
        output = ctx.stage_dir("04_audio")
        wav = output / "audio_16k.wav"
        wav.write_bytes(b"RIFF-audio")
        ctx.save_json(
            output / "audio.json",
            {
                "has_audio": True,
                "path": "04_audio/audio_16k.wav",
            },
        )
        return {"has_audio": True}

    return {
        "01_probe": SimpleNamespace(NAME="01_probe", run=probe),
        "02_scenes": SimpleNamespace(NAME="02_scenes", run=scenes),
        "03_keyframes": SimpleNamespace(NAME="03_keyframes", run=keyframes),
        "04_audio": SimpleNamespace(NAME="04_audio", run=audio),
    }


def create_runtime(tmp_path: Path, seen=None):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake-video")
    output = tmp_path / "output"
    context = PipelineContext(video_path=video, out_root=output)
    store = LocalArtifactStore(output, namespace="run-123")
    registrar = LegacyOutputAdapter(store)
    bindings = create_legacy_media_bindings(
        context,
        registrar,
        stage_modules=fake_modules([] if seen is None else seen),
    )
    return context, store, bindings, path_ref(video, "video")


def test_media_bindings_execute_legacy_stages_and_publish_artifacts(
    tmp_path: Path,
) -> None:
    seen = []
    context, store, bindings, video = create_runtime(tmp_path, seen)

    async def scenario():
        executor = LocalExecutor(bindings)
        first_task = task("01_probe", "1.0.0", {"video": video})
        first = await executor.result(await executor.submit(first_task))
        second_task = task(
            "02_scenes",
            "1.0.0",
            {"video": video, "metadata": first.outputs["metadata"]},
            config={
                "scene_threshold": 31.5,
                "min_scene_len_frames": 20,
            },
        )
        second = await executor.result(await executor.submit(second_task))
        third_task = task(
            "03_keyframes",
            "1.2.0",
            {"video": video, "scenes": second.outputs["scenes"]},
            config={"keyframes_per_scene": 2},
        )
        third = await executor.result(await executor.submit(third_task))
        fourth_task = task(
            "04_audio",
            "1.0.0",
            {"video": video, "metadata": first.outputs["metadata"]},
        )
        fourth = await executor.result(await executor.submit(fourth_task))
        return first, second, third, fourth

    first, second, third, fourth = asyncio.run(scenario())

    assert bindings.names == (
        "01_probe",
        "02_scenes",
        "03_keyframes",
        "04_audio",
    )
    assert [result.status for result in (first, second, third, fourth)] == [
        StageStatus.SUCCEEDED,
    ] * 4
    assert set(first.outputs) == {"metadata"}
    assert set(second.outputs) == {"scenes", "scene_stats"}
    assert set(third.outputs) == {"keyframes", "keyframe_images"}
    assert set(fourth.outputs) == {"audio", "audio_metadata"}
    for result in (first, second, third, fourth):
        assert all(store.verify(ref).ok for ref in result.outputs.values())
    assert seen == [
        ("01_probe", None),
        ("02_scenes", (31.5, 20)),
        ("03_keyframes", 2),
        ("04_audio", None),
    ]
    assert context.scene_threshold == 27.0
    assert context.min_scene_len_frames == 15
    assert context.keyframes_per_scene == 1

    bundle = context.out_root / "03_keyframes" / "keyframe_images.zip"
    with zipfile.ZipFile(bundle) as archive:
        assert archive.namelist() == [
            "03_keyframes/frames/scene_001.jpg",
            "03_keyframes/frames/scene_002.jpg",
        ]
        assert archive.read(archive.namelist()[0]) == b"first-image"


def test_pipeline_engine_runs_first_four_legacy_bindings(
    tmp_path: Path,
) -> None:
    context, _, bindings, video = create_runtime(tmp_path)
    first_four = DEFAULT_STAGE_SPECS[:4]
    execution_plan = DAGPlanner(
        StageRegistry(first_four, external_inputs=("video",))
    ).plan()
    engine = PipelineEngine(LocalExecutor(bindings))

    result = asyncio.run(
        engine.run(
            execution_plan,
            run_id="run-123",
            trace_id="trace-123",
            artifacts={"video": video},
            stage_configs={
                "02_scenes": {
                    "scene_threshold": 29.0,
                    "min_scene_len_frames": 18,
                },
                "03_keyframes": {"keyframes_per_scene": 1},
            },
        )
    )

    assert result.status.value == "succeeded"
    assert [record.stage for record in result.stages] == [
        "01_probe",
        "02_scenes",
        "03_keyframes",
        "04_audio",
    ]
    assert set(result.artifacts) >= {
        "metadata",
        "scenes",
        "scene_stats",
        "keyframes",
        "keyframe_images",
        "audio",
        "audio_metadata",
    }
    assert context.scene_threshold == 27.0


def test_distinct_legacy_stages_can_overlap_with_separate_config_fields(
    tmp_path: Path,
) -> None:
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    context = PipelineContext(video_path=video, out_root=tmp_path / "output")
    barrier = threading.Barrier(2)
    seen = []

    def visual(ctx):
        seen.append(("visual", ctx.scene_threshold))
        barrier.wait(timeout=1)
        return {}

    def audio(ctx):
        seen.append(("audio", ctx.vad_min_silence_ms))
        barrier.wait(timeout=1)
        return {}

    class Registrar:
        def register_file(self, *args, **kwargs):
            raise AssertionError("test stages do not publish files")

    definitions = (
        LegacyStageDefinition(
            name="visual",
            stage_version="1.0.0",
            module=SimpleNamespace(NAME="visual", run=visual),
            inputs=(),
            config_fields=("scene_threshold",),
            model_bindings={},
            output_resolver=lambda ctx, registrar, stage_task: {},
        ),
        LegacyStageDefinition(
            name="audio",
            stage_version="1.0.0",
            module=SimpleNamespace(NAME="audio", run=audio),
            inputs=(),
            config_fields=("vad_min_silence_ms",),
            model_bindings={},
            output_resolver=lambda ctx, registrar, stage_task: {},
        ),
    )
    bindings = _create_binding_registry(context, Registrar(), definitions)

    async def scenario():
        executor = LocalExecutor(bindings, max_concurrency=2)
        handles = (
            await executor.submit(
                task(
                    "visual",
                    "1.0.0",
                    {},
                    config={"scene_threshold": 31.5},
                )
            ),
            await executor.submit(
                task(
                    "audio",
                    "1.0.0",
                    {},
                    config={"vad_min_silence_ms": 750},
                )
            ),
        )
        return await asyncio.gather(
            *(executor.result(handle) for handle in handles)
        )

    results = asyncio.run(scenario())

    assert [result.status for result in results] == [
        StageStatus.SUCCEEDED,
        StageStatus.SUCCEEDED,
    ]
    assert set(seen) == {("visual", 31.5), ("audio", 750)}
    assert context.scene_threshold == 27.0
    assert context.vad_min_silence_ms == 500


def test_no_audio_uses_metadata_as_a_sentinel_and_ignores_stale_wav(
    tmp_path: Path,
) -> None:
    context, _, bindings, video = create_runtime(tmp_path)
    probe_dir = context.stage_dir("01_probe")
    context.save_json(
        probe_dir / "metadata.json",
        {"summary": {"audio": None}},
    )
    registrar = context.artifact_registrar
    metadata = registrar.register_file(
        "01_probe/metadata.json",
        artifact_id="metadata",
        kind="json",
        media_type="application/json",
    )
    audio_dir = context.stage_dir("04_audio")
    (audio_dir / "audio_16k.wav").write_bytes(b"stale-audio")

    def no_audio(ctx):
        ctx.save_json(
            ctx.stage_dir("04_audio") / "audio.json",
            {"has_audio": False},
        )
        return {"has_audio": False}

    runner = create_legacy_media_bindings(
        context,
        registrar,
        stage_modules={
            **fake_modules([]),
            "04_audio": SimpleNamespace(NAME="04_audio", run=no_audio),
        },
    ).get("04_audio")
    result = runner(
        task(
            "04_audio",
            "1.0.0",
            {"video": video, "metadata": metadata},
        )
    )

    assert result.outputs["audio"] == result.outputs["audio_metadata"]
    assert result.outputs["audio"].media_type == "application/json"


@pytest.mark.parametrize(
    "task_override",
    [
        {"config": {"scene_threshold": 27.0}},
        {"model_bindings": {"scene": "scene.default"}},
    ],
)
def test_binding_rejects_hidden_or_unexpected_task_configuration(
    tmp_path: Path,
    task_override: dict[str, object],
) -> None:
    context, _, bindings, video = create_runtime(tmp_path)
    context.save_json(
        context.stage_dir("01_probe") / "metadata.json",
        {"summary": {}},
    )
    metadata = context.artifact_registrar.register_file(
        "01_probe/metadata.json",
        artifact_id="metadata",
        kind="json",
        media_type="application/json",
    )
    kwargs = {
        "config": {
            "scene_threshold": 27.0,
            "min_scene_len_frames": 15,
        },
        "model_bindings": {},
    }
    kwargs.update(task_override)

    with pytest.raises(LegacyStageContractError, match="config|model"):
        bindings.get("02_scenes")(
            task(
                "02_scenes",
                "1.0.0",
                {"video": video, "metadata": metadata},
                **kwargs,
            )
        )


def test_binding_rejects_materialized_input_that_changed_after_reference(
    tmp_path: Path,
) -> None:
    context, _, bindings, video = create_runtime(tmp_path)
    metadata_path = context.stage_dir("01_probe") / "metadata.json"
    context.save_json(metadata_path, {"summary": {}})
    metadata = context.artifact_registrar.register_file(
        "01_probe/metadata.json",
        artifact_id="metadata",
        kind="json",
        media_type="application/json",
    )
    metadata_path.write_text("changed", encoding="utf-8")

    with pytest.raises(LegacyStageContractError, match="ArtifactRef"):
        bindings.get("02_scenes")(
            task(
                "02_scenes",
                "1.0.0",
                {"video": video, "metadata": metadata},
                config={
                    "scene_threshold": 27.0,
                    "min_scene_len_frames": 15,
                },
            )
        )


def test_keyframe_bundle_is_deterministic_for_same_images(
    tmp_path: Path,
) -> None:
    context, _, bindings, video = create_runtime(tmp_path)
    scenes_path = context.stage_dir("02_scenes") / "scenes.json"
    context.save_json(scenes_path, {"scenes": []})
    scenes = context.artifact_registrar.register_file(
        "02_scenes/scenes.json",
        artifact_id="scenes",
        kind="json",
        media_type="application/json",
    )
    runner = bindings.get("03_keyframes")
    stage_task = task(
        "03_keyframes",
        "1.2.0",
        {"video": video, "scenes": scenes},
        config={"keyframes_per_scene": 1},
    )

    first = runner(stage_task).outputs["keyframe_images"].checksum
    second = runner(stage_task).outputs["keyframe_images"].checksum

    assert first == second
