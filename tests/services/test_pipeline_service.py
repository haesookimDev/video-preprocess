"""Tests for the shared pipeline Application Service boundary."""

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from video_preprocess.domain import (
    ArtifactRef,
    Checksum,
    RunStatus,
    StageSpec,
)
from video_preprocess.engine import (
    DAGPlanner,
    PipelinePreviewResult,
    PipelineRunResult,
    StageRegistry,
    create_default_registry,
)
from video_preprocess.services import (
    HTTPProviderSettings,
    InferenceDeploymentSettings,
    LocalPipelineRuntimeFactory,
    PipelineApplicationService,
    PipelineRunRequest,
    PipelineRuntime,
    PipelineServiceInputError,
    PipelineSettings,
)


def artifact(name: str, payload: bytes = b"data") -> ArtifactRef:
    return ArtifactRef(
        artifact_id=name,
        kind="video" if name == "video" else "json",
        uri=f"artifact://test/{name}",
        media_type=(
            "video/mp4" if name == "video" else "application/json"
        ),
        size_bytes=len(payload),
        checksum=Checksum("sha256", hashlib.sha256(payload).hexdigest()),
    )


class RecordingEngine:
    def __init__(self):
        self.calls = []

    async def run(self, plan, **options):
        self.calls.append((plan, options))
        return PipelineRunResult(
            run_id=options["run_id"],
            status=RunStatus.SUCCEEDED,
            stages=(),
            artifacts=dict(options["artifacts"]),
            transitions=(RunStatus.PENDING, RunStatus.RUNNING, RunStatus.SUCCEEDED),
        )

    async def preview(self, plan, **options):
        self.calls.append((plan, options))
        return PipelinePreviewResult(
            run_id=options["run_id"],
            stages=(),
            artifacts=dict(options["artifacts"]),
        )


class RecordingRuntimeFactory:
    def __init__(self, engine, artifacts):
        self.engine = engine
        self.artifacts = artifacts
        self.calls = []

    def create(self, request, *, run_id, boundary_inputs):
        self.calls.append((request, run_id, tuple(boundary_inputs)))
        return PipelineRuntime(self.engine, self.artifacts)

    def create_preview(self, request, *, run_id, boundary_inputs):
        self.calls.append((request, run_id, tuple(boundary_inputs)))
        return PipelineRuntime(self.engine, self.artifacts)


def test_pipeline_settings_normalizes_ocr_contract() -> None:
    settings = PipelineSettings(
        ocr_mode="caption-hints",
        ocr_model="custom/ocr",
        ocr_languages=(" ENG ", "kor", "eng"),
        ocr_detect_orientation=False,
        ocr_min_confidence=0.75,
    )

    assert settings.ocr_languages == ("eng", "kor")
    assert settings.stage_configs()["08_ocr"] == {
        "ocr_mode": "caption-hints",
        "ocr_model": "custom/ocr",
        "ocr_languages": ["eng", "kor"],
        "ocr_detect_orientation": False,
        "ocr_min_confidence": 0.75,
    }
    assert settings.model_bindings()["08_ocr"] == {"ocr": "ocr.default"}


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"ocr_mode": "sometimes"}, "ocr_mode"),
        ({"ocr_mode": []}, "ocr_mode"),
        ({"ocr_languages": ()}, "ocr_languages"),
        ({"ocr_languages": ("eng-US",)}, "ocr_languages"),
        ({"ocr_detect_orientation": 1}, "ocr_detect_orientation"),
        ({"ocr_min_confidence": 1.1}, "ocr_min_confidence"),
    ],
)
def test_pipeline_settings_rejects_invalid_ocr_contract(
    options,
    message,
) -> None:
    with pytest.raises(ValueError, match=message):
        PipelineSettings(**options)


def test_service_plans_selected_stage_and_forwards_exact_settings(
    tmp_path: Path,
) -> None:
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    engine = RecordingEngine()
    runtime_factory = RecordingRuntimeFactory(
        engine,
        {"timeline": artifact("timeline")},
    )
    service = PipelineApplicationService(
        DAGPlanner(create_default_registry()),
        runtime_factory,
        run_id_factory=lambda: "generated-run",
        trace_id_factory=lambda: "generated-trace",
    )
    request = PipelineRunRequest(
        video_path=video,
        output_root=tmp_path / "output",
        stage="10_index",
        force_stages=("10_index",),
        settings=PipelineSettings(embed_model="custom/embedding"),
    )

    result = asyncio.run(service.run(request))

    assert result.status is RunStatus.SUCCEEDED
    assert runtime_factory.calls == [
        (request, "generated-run", ("timeline",))
    ]
    plan, options = engine.calls[0]
    assert plan.stage_names == ("10_index",)
    assert options["run_id"] == "generated-run"
    assert options["trace_id"] == "generated-trace"
    assert options["stage_configs"] == {
        "10_index": {"embed_model": "custom/embedding"}
    }
    assert options["model_bindings"] == {
        "10_index": {"embedding": "embedding.default"}
    }
    assert options["force_stages"] == ("10_index",)
    assert options["stage_timeouts"] == {}
    assert options["retry_policy"].max_attempts == 1
    assert options["cancellation"] is None


def test_service_forwards_execution_policy_to_each_planned_stage(
    tmp_path: Path,
) -> None:
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    engine = RecordingEngine()
    factory = RecordingRuntimeFactory(
        engine,
        {"video": artifact("video")},
    )
    service = PipelineApplicationService(
        DAGPlanner(create_default_registry()),
        factory,
    )

    asyncio.run(
        service.run(
            PipelineRunRequest(
                video_path=video,
                output_root=tmp_path / "output",
                to_stage="02_scenes",
                stage_timeout_sec=30,
                max_stage_attempts=3,
                retry_backoff_sec=0.25,
            )
        )
    )

    _, options = engine.calls[0]
    assert options["stage_timeouts"] == {
        "01_probe": 30.0,
        "02_scenes": 30.0,
    }
    assert options["retry_policy"].max_attempts == 3
    assert options["retry_policy"].initial_backoff_sec == 0.25


def test_service_rejects_missing_file_and_unresolved_boundary(
    tmp_path: Path,
) -> None:
    engine = RecordingEngine()
    factory = RecordingRuntimeFactory(engine, {"video": artifact("video")})
    service = PipelineApplicationService(
        DAGPlanner(create_default_registry()),
        factory,
    )
    missing_request = PipelineRunRequest(
        video_path=tmp_path / "missing.mp4",
        output_root=tmp_path / "output",
    )
    with pytest.raises(PipelineServiceInputError, match="does not exist"):
        asyncio.run(service.run(missing_request))

    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    partial_request = PipelineRunRequest(
        video_path=video,
        output_root=tmp_path / "output",
        stage="10_index",
    )
    with pytest.raises(PipelineServiceInputError, match="timeline"):
        asyncio.run(service.run(partial_request))


def test_service_filters_configs_to_custom_plan(tmp_path: Path) -> None:
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    registry = StageRegistry(
        (
            StageSpec(
                name="first",
                stage_version="1.0.0",
                required_inputs=("video",),
                outputs=("middle",),
            ),
            StageSpec(
                name="second",
                stage_version="1.0.0",
                dependencies=("first",),
                required_inputs=("middle",),
                outputs=("final",),
            ),
        ),
        external_inputs=("video",),
    )
    engine = RecordingEngine()
    factory = RecordingRuntimeFactory(engine, {"video": artifact("video")})
    service = PipelineApplicationService(DAGPlanner(registry), factory)

    asyncio.run(
        service.run(
            PipelineRunRequest(
                video_path=video,
                output_root=tmp_path / "output",
                run_id="run-123",
                trace_id="trace-123",
            )
        )
    )

    _, options = engine.calls[0]
    assert options["stage_configs"] == {}
    assert options["model_bindings"] == {}


def test_local_runtime_ingests_video_and_composes_all_bindings(
    tmp_path: Path,
) -> None:
    video = tmp_path / "source.mp4"
    video.write_bytes(b"video-bytes")
    seen = []
    names = (
        "01_probe",
        "02_scenes",
        "03_keyframes",
        "04_audio",
        "05_vad",
        "06_stt",
        "07_diarize",
        "08_captions",
        "08_ocr",
        "09_timeline",
        "10_index",
        "11_context",
    )
    modules = {
        name: SimpleNamespace(NAME=name, run=lambda ctx: {})
        for name in names
    }

    def configure(context, store):
        seen.append((context, store))

    request = PipelineRunRequest(
        video_path=video,
        output_root=tmp_path / "output",
        settings=PipelineSettings(scene_threshold=31.5),
    )
    runtime = LocalPipelineRuntimeFactory(
        stage_modules=modules,
        context_configurer=configure,
        executor_max_concurrency=3,
    ).create(request, run_id="run-123", boundary_inputs=("video",))

    context, store = seen[0]
    assert runtime.engine.executor.bindings.names == names
    assert runtime.engine.executor.max_concurrency == 3
    assert context.scene_threshold == 31.5
    assert context.video_path == video.resolve()
    assert store.verify(runtime.artifacts["video"]).ok
    assert (request.output_root / "00_input" / "video.mp4").read_bytes() == (
        b"video-bytes"
    )


@pytest.mark.parametrize("max_concurrency", [True, 0, -1, 1.5])
def test_local_runtime_rejects_invalid_executor_concurrency(
    max_concurrency,
) -> None:
    with pytest.raises(ValueError, match="executor_max_concurrency"):
        LocalPipelineRuntimeFactory(
            executor_max_concurrency=max_concurrency,
        )


@pytest.mark.parametrize("batch_size", [True, 0, -1, 1.5])
def test_local_runtime_rejects_invalid_caption_batch_size(batch_size) -> None:
    with pytest.raises(ValueError, match="caption_batch_size"):
        LocalPipelineRuntimeFactory(caption_batch_size=batch_size)


@pytest.mark.parametrize("batch_size", [True, 0, -1, 1.5])
def test_local_runtime_rejects_invalid_ocr_batch_size(batch_size) -> None:
    with pytest.raises(ValueError, match="ocr_batch_size"):
        LocalPipelineRuntimeFactory(ocr_batch_size=batch_size)


@pytest.mark.parametrize("command", ["", "   ", None, 1])
def test_local_runtime_rejects_invalid_ocr_command(command) -> None:
    with pytest.raises(ValueError, match="ocr_command"):
        LocalPipelineRuntimeFactory(ocr_command=command)


@pytest.mark.parametrize("device", ["", "   ", None, 1])
def test_local_runtime_rejects_invalid_caption_device(device) -> None:
    with pytest.raises(ValueError, match="caption_device"):
        LocalPipelineRuntimeFactory(caption_device=device)


def test_local_runtime_keeps_caption_tuning_in_composition() -> None:
    factory = LocalPipelineRuntimeFactory(
        caption_device="MPS",
        caption_batch_size=2,
    )

    assert factory.caption_device == "mps"
    assert factory.caption_batch_size == 2


def test_local_runtime_keeps_ocr_tuning_in_composition() -> None:
    factory = LocalPipelineRuntimeFactory(
        ocr_command="custom-tesseract",
        ocr_batch_size=2,
    )

    assert factory.ocr_command == "custom-tesseract"
    assert factory.ocr_batch_size == 2


def test_local_runtime_requires_manifest_for_partial_execution(
    tmp_path: Path,
) -> None:
    video = tmp_path / "source.mp4"
    video.write_bytes(b"video")
    request = PipelineRunRequest(
        video_path=video,
        output_root=tmp_path / "output",
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
        "08_ocr",
        "09_timeline",
        "10_index",
        "11_context",
    )
    modules = {
        name: SimpleNamespace(NAME=name, run=lambda ctx: {})
        for name in names
    }
    factory = LocalPipelineRuntimeFactory(
        stage_modules=modules,
        context_configurer=lambda context, store: None,
    )

    with pytest.raises(PipelineServiceInputError, match="previous run"):
        factory.create(
            request,
            run_id="run-123",
            boundary_inputs=("timeline",),
        )


def test_local_preview_is_read_only_and_reports_missing_boundary(
    tmp_path: Path,
) -> None:
    video = tmp_path / "source.mp4"
    video.write_bytes(b"video")
    output = tmp_path / "output"
    service = PipelineApplicationService(
        DAGPlanner(create_default_registry()),
        LocalPipelineRuntimeFactory(),
    )

    result = asyncio.run(
        service.preview(
            PipelineRunRequest(
                video_path=video,
                output_root=output,
                run_id="run-123",
                trace_id="trace-123",
                stage="10_index",
            )
        )
    )

    assert result.stages[0].status.value == "blocked"
    assert result.stages[0].blocked_inputs == ("timeline",)
    assert not output.exists()


def test_local_runtime_composes_remote_embedding_alias_without_loading_model(
    tmp_path: Path,
) -> None:
    video = tmp_path / "source.mp4"
    video.write_bytes(b"video")
    deployments = InferenceDeploymentSettings(
        http_providers={
            "embedding.default": HTTPProviderSettings(
                endpoint="https://models.example.test"
            )
        }
    )

    runtime = LocalPipelineRuntimeFactory().create_preview(
        PipelineRunRequest(
            video_path=video,
            output_root=tmp_path / "output",
            deployments=deployments,
        ),
        run_id="run-123",
        boundary_inputs=("video",),
    )

    resolver = runtime.engine.model_resolver
    assert resolver is not None
    gateway = resolver.gateways["embedding.default"]
    provider = gateway._bindings["embedding.default"]
    assert provider.__class__.__name__ == "HTTPInferenceProvider"
    assert not (tmp_path / "output").exists()


def test_local_runtime_composes_remote_ocr_alias_without_local_command(
    tmp_path: Path,
) -> None:
    video = tmp_path / "source.mp4"
    video.write_bytes(b"video")
    deployments = InferenceDeploymentSettings(
        http_providers={
            "ocr.default": HTTPProviderSettings(
                endpoint="https://ocr.example.test"
            )
        }
    )

    runtime = LocalPipelineRuntimeFactory(
        ocr_command="missing-local-command",
    ).create_preview(
        PipelineRunRequest(
            video_path=video,
            output_root=tmp_path / "output",
            deployments=deployments,
        ),
        run_id="run-123",
        boundary_inputs=("video",),
    )

    resolver = runtime.engine.model_resolver
    assert resolver is not None
    gateway = resolver.gateways["ocr.default"]
    provider = gateway._bindings["ocr.default"]
    assert provider.__class__.__name__ == "HTTPInferenceProvider"
    assert not (tmp_path / "output").exists()
