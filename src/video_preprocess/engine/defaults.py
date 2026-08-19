"""Stable StageSpec registry for the current twelve-stage pipeline."""

from video_preprocess.domain import ResourceHints, StageSpec

from .registry import StageRegistry


DEFAULT_STAGE_SPECS = (
    StageSpec(
        name="01_probe",
        stage_version="1.0.0",
        required_inputs=("video",),
        outputs=("metadata",),
        resource_hints=ResourceHints(cpu=0.5, memory_mb=128),
    ),
    StageSpec(
        name="02_scenes",
        stage_version="1.0.0",
        dependencies=("01_probe",),
        required_inputs=("video", "metadata"),
        outputs=("scenes", "scene_stats"),
        resource_hints=ResourceHints(cpu=1.0, memory_mb=512),
    ),
    StageSpec(
        name="03_keyframes",
        stage_version="1.3.0",
        dependencies=("02_scenes",),
        required_inputs=("video", "scenes"),
        outputs=("keyframes", "keyframe_images"),
        resource_hints=ResourceHints(cpu=1.0, memory_mb=256),
    ),
    StageSpec(
        name="04_audio",
        stage_version="1.0.0",
        dependencies=("01_probe",),
        required_inputs=("video", "metadata"),
        outputs=("audio", "audio_metadata"),
        resource_hints=ResourceHints(cpu=1.0, memory_mb=256),
    ),
    StageSpec(
        name="05_vad",
        stage_version="1.0.0",
        dependencies=("04_audio",),
        required_inputs=("audio", "audio_metadata"),
        outputs=("vad_segments",),
        model_slots=("vad",),
        resource_hints=ResourceHints(
            cpu=1.0,
            memory_mb=1024,
            gpu_optional=False,
        ),
    ),
    StageSpec(
        name="06_stt",
        stage_version="1.0.0",
        dependencies=("04_audio", "05_vad"),
        required_inputs=("audio", "vad_segments"),
        outputs=("transcript",),
        model_slots=("stt",),
        resource_hints=ResourceHints(
            cpu=2.0,
            memory_mb=4096,
            gpu_optional=True,
        ),
    ),
    StageSpec(
        name="07_diarize",
        stage_version="1.0.0",
        dependencies=("04_audio",),
        required_inputs=("audio",),
        outputs=("diarization",),
        model_slots=("diarization",),
        resource_hints=ResourceHints(
            cpu=2.0,
            memory_mb=4096,
            gpu_optional=True,
        ),
    ),
    StageSpec(
        name="08_captions",
        stage_version="1.3.0",
        dependencies=("03_keyframes",),
        required_inputs=("keyframes", "keyframe_images"),
        outputs=("captions",),
        model_slots=("caption",),
        resource_hints=ResourceHints(
            cpu=2.0,
            memory_mb=4096,
            gpu_optional=True,
        ),
    ),
    StageSpec(
        name="08_ocr",
        stage_version="1.0.0",
        dependencies=("03_keyframes", "08_captions"),
        required_inputs=("keyframes", "keyframe_images", "captions"),
        outputs=("ocr",),
        model_slots=("ocr",),
        resource_hints=ResourceHints(
            cpu=2.0,
            memory_mb=1024,
            gpu_optional=False,
        ),
    ),
    StageSpec(
        name="09_timeline",
        stage_version="1.3.0",
        dependencies=(
            "02_scenes",
            "03_keyframes",
            "06_stt",
            "07_diarize",
            "08_captions",
            "08_ocr",
        ),
        required_inputs=(
            "scenes",
            "keyframes",
            "transcript",
            "diarization",
            "captions",
            "ocr",
        ),
        outputs=("timeline", "timeline_markdown"),
        resource_hints=ResourceHints(cpu=0.5, memory_mb=256),
    ),
    StageSpec(
        name="10_index",
        stage_version="1.2.0",
        dependencies=("09_timeline",),
        required_inputs=("timeline",),
        outputs=("search_index", "index_summary"),
        model_slots=("embedding",),
        resource_hints=ResourceHints(
            cpu=2.0,
            memory_mb=2048,
            gpu_optional=True,
        ),
    ),
    StageSpec(
        name="11_context",
        stage_version="1.2.0",
        dependencies=("01_probe", "07_diarize", "09_timeline"),
        required_inputs=("metadata", "diarization", "timeline"),
        outputs=("context", "context_json"),
        resource_hints=ResourceHints(cpu=0.5, memory_mb=256),
    ),
)


def create_default_registry() -> StageRegistry:
    """Return the validated current-pipeline registry."""

    return StageRegistry(DEFAULT_STAGE_SPECS, external_inputs=("video",))
