"""Compatibility tests for the optional provider-backed OCR Stage."""

from pathlib import Path

from pipeline.context import PipelineContext
from pipeline.stages import s08_ocr
from video_preprocess.domain import EffectiveModel
from video_preprocess.inference import OCRBatch, OCRImageResult, OCRRegion
from video_preprocess.storage import LocalArtifactStore, LegacyOutputAdapter


class FakeOCRService:
    def __init__(self) -> None:
        self.images = []
        self.options = {}

    def recognize(self, images, **options) -> OCRBatch:
        self.images = list(images)
        self.options = options
        results = tuple(
            OCRImageResult(
                artifact_id=image.artifact_id,
                text=f"screen {index}",
                image_width=640,
                image_height=360,
                regions=(
                    OCRRegion(
                        region_id=1,
                        text="screen",
                        confidence=0.95,
                        x=10,
                        y=20,
                        width=100,
                        height=30,
                    ),
                ),
            )
            for index, image in enumerate(self.images, start=1)
        )
        return OCRBatch(
            results=results,
            model=EffectiveModel(
                provider="fake.ocr",
                name="fake/model",
                revision="rev-1",
                runtime="fake/1.0",
            ),
            usage={"image_count": len(results)},
            timing={"inference_sec": 0.01},
        )


def _context(tmp_path: Path, *, mode: str) -> PipelineContext:
    context = PipelineContext(
        video_path=tmp_path / "sample.mp4",
        out_root=tmp_path / "output" / "sample",
        ocr_mode=mode,
        ocr_model="fake/model",
        ocr_languages=("eng", "kor"),
        ocr_detect_orientation=False,
        ocr_min_confidence=0.7,
    )
    paths = (
        "03_keyframes/frames/scene_001_01.jpg",
        "03_keyframes/frames/scene_001_02.jpg",
        "03_keyframes/frames/scene_002.jpg",
    )
    for index, relative_path in enumerate(paths, start=1):
        path = context.out_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"frame-{index}".encode())
    context.save_json(
        context.stage_dir("03_keyframes") / "keyframes.json",
        {
            "keyframes": [
                {
                    "scene_id": 1,
                    "keyframe_index": 1,
                    "keyframe_count": 2,
                    "timestamp_sec": 2.0,
                    "path": paths[0],
                },
                {
                    "scene_id": 1,
                    "keyframe_index": 2,
                    "keyframe_count": 2,
                    "timestamp_sec": 4.0,
                    "path": paths[1],
                },
                {
                    "scene_id": 2,
                    "keyframe_index": 1,
                    "keyframe_count": 1,
                    "timestamp_sec": 8.0,
                    "path": paths[2],
                },
            ]
        },
    )
    return context


def _compose(context: PipelineContext) -> FakeOCRService:
    service = FakeOCRService()
    store = LocalArtifactStore(context.out_root, namespace="sample")
    context.ocr_service = service
    context.artifact_registrar = LegacyOutputAdapter(store)
    return service


def test_ocr_stage_processes_all_keyframes_in_stable_order(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path, mode="all")
    service = _compose(context)

    metrics = s08_ocr.run(context)

    output = context.load_json(context.out_root / "08_ocr" / "ocr.json")
    assert metrics == {
        "ocr_image_count": 3,
        "ocr_text_frame_count": 3,
        "ocr_region_count": 3,
    }
    assert [image.artifact_id for image in service.images] == [
        "ocr_keyframe_scene_001_01",
        "ocr_keyframe_scene_001_02",
        "ocr_keyframe_scene_002",
    ]
    assert service.options["languages"] == ("eng", "kor")
    assert service.options["detect_orientation"] is False
    assert service.options["min_confidence"] == 0.7
    assert output["provider"] == "fake.ocr"
    assert output["revision"] == "rev-1"
    assert output["candidate_count"] == 3
    assert output["results"][0]["regions"][0]["bbox"] == {
        "x": 10,
        "y": 20,
        "width": 100,
        "height": 30,
    }


def test_reprocessing_ocr_only_infers_selected_scene(tmp_path: Path) -> None:
    context = _context(tmp_path, mode="all")
    context.reprocessing_source_run_id = "run-source"
    context.reprocessing_profile = "visual-detail-v1"
    context.reprocessing_scene_ids = (2,)
    context.reprocessing_overlay_policy = "copy-unselected-from-source-v1"
    source_ocr = context.out_root / "00_source" / "08_ocr" / "ocr.json"
    source_ocr.parent.mkdir(parents=True)
    context.save_json(
        source_ocr,
        {
            "results": [
                {
                    "scene_id": 1,
                    "keyframe_index": index,
                    "keyframe_count": 2,
                    "timestamp_sec": float(index * 2),
                    "keyframe": (
                        f"03_keyframes/frames/scene_001_{index:02d}.jpg"
                    ),
                    "text": f"source {index}",
                    "image_width": 640,
                    "image_height": 360,
                    "regions": [],
                }
                for index in (1, 2)
            ]
        },
    )
    service = _compose(context)

    metrics = s08_ocr.run(context)

    output = context.load_json(context.out_root / "08_ocr" / "ocr.json")
    assert len(service.images) == 1
    assert service.images[0].metadata["scene_id"] == 2
    assert [item["text"] for item in output["results"]] == [
        "source 1",
        "source 2",
        "screen 1",
    ]
    assert [
        item["reprocessing"]["origin"] for item in output["results"]
    ] == ["source", "source", "selected-pass"]
    assert metrics["processed_ocr_image_count"] == 1
    assert metrics["reused_ocr_image_count"] == 2


def test_caption_hint_mode_only_processes_matching_keyframes(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path, mode="caption-hints")
    context.save_json(
        context.stage_dir("08_captions") / "captions.json",
        {
            "captions": [
                {
                    "scene_id": 1,
                    "keyframe_index": 1,
                    "caption": "a presenter",
                },
                {
                    "scene_id": 1,
                    "keyframe_index": 2,
                    "caption": "a title on a slide",
                },
                {
                    "scene_id": 2,
                    "keyframe_index": 1,
                    "caption": "a person by a sign",
                },
            ]
        },
    )
    service = _compose(context)

    metrics = s08_ocr.run(context)

    output = context.load_json(context.out_root / "08_ocr" / "ocr.json")
    assert metrics["ocr_image_count"] == 2
    assert [image.artifact_id for image in service.images] == [
        "ocr_keyframe_scene_001_02",
        "ocr_keyframe_scene_002",
    ]
    assert [result["trigger_hint"] for result in output["results"]] == [
        "title",
        "sign",
    ]
    assert output["trigger_hint_policy"] == "caption-keyword-hints-v1"


def test_disabled_ocr_writes_stable_skip_without_inference_dependencies(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path, mode="disabled")

    metrics = s08_ocr.run(context)

    output = context.load_json(context.out_root / "08_ocr" / "ocr.json")
    assert metrics["skipped"] == "OCR_DISABLED"
    assert output["enabled"] is False
    assert output["executed"] is False
    assert output["reason_code"] == "OCR_DISABLED"
    assert output["results"] == []


def test_caption_hint_mode_skips_when_no_caption_matches(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path, mode="caption-hints")
    context.save_json(
        context.stage_dir("08_captions") / "captions.json",
        {
            "captions": [
                {"scene_id": 1, "keyframe_index": 1, "caption": "a person"},
                {
                    "scene_id": 1,
                    "keyframe_index": 2,
                    "caption": "a context view",
                },
                {"scene_id": 2, "keyframe_index": 1, "caption": "a room"},
            ]
        },
    )

    metrics = s08_ocr.run(context)

    output = context.load_json(context.out_root / "08_ocr" / "ocr.json")
    assert metrics["skipped"] == "NO_OCR_CANDIDATES"
    assert output["candidate_count"] == 0
    assert output["executed"] is False
