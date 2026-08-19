"""Contract tests for ordered local OCR inference."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pytest

from video_preprocess.domain import HealthState, InferenceErrorCode
from video_preprocess.inference import InferenceCallError, OCRService
from video_preprocess.inference.gateway import InferenceGateway
from video_preprocess.inference.local.ocr import (
    LocalOCRProvider,
    OCRCommandResult,
)
from video_preprocess.storage import LocalArtifactStore


def publish_image(
    store: LocalArtifactStore,
    name: str,
    payload: bytes,
):
    pending = store.put(
        stream=io.BytesIO(payload),
        artifact_id=f"image-{name}",
        relative_path=f"frames/{name}.jpg",
        kind="image",
        media_type="image/jpeg",
    )
    return store.publish(pending)


def tsv_for(
    word: str,
    *,
    confidence: int = 95,
    left: int = 10,
    width: int = 30,
) -> bytes:
    return (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
        "left\ttop\twidth\theight\tconf\ttext\n"
        "1\t1\t0\t0\t0\t0\t0\t0\t100\t40\t-1\t\n"
        f"5\t1\t1\t1\t1\t1\t{left}\t5\t{width}\t10\t"
        f"{confidence}\t{word}\n"
        "5\t1\t1\t1\t1\t2\t45\t5\t20\t10\t30\tignored\n"
    ).encode("utf-8")


def make_service(
    store: LocalArtifactStore,
    *,
    runner,
    version_resolver=lambda command: "5.5.0",
    provider_batch_size: int = 2,
    service_batch_size: int | None = 2,
):
    provider = LocalOCRProvider(
        alias="ocr.default",
        model_name="tesseract",
        revision="system",
        command="fake-tesseract",
        artifact_store=store,
        max_batch_size=provider_batch_size,
        process_runner=runner,
        version_resolver=version_resolver,
    )
    service = OCRService(
        InferenceGateway({"ocr.default": provider}),
        alias="ocr.default",
        model_name="tesseract",
        revision="system",
        batch_size=service_batch_size,
    )
    return provider, service


def test_service_chunks_ordered_images_and_caches_provider_results(
    tmp_path: Path,
) -> None:
    store = LocalArtifactStore(tmp_path, namespace="run-ocr")
    images = [
        publish_image(store, name, name.encode("utf-8"))
        for name in ("one", "two", "three")
    ]
    calls = []

    def runner(arguments, payload, timeout_sec):
        calls.append((arguments, payload, timeout_sec))
        return OCRCommandResult(0, tsv_for(payload.decode("utf-8")), b"")

    provider, service = make_service(store, runner=runner)

    first = service.recognize(
        images,
        languages=("eng", "kor", "eng"),
        min_confidence=0.5,
    )
    second = service.recognize(
        images,
        languages=("eng", "kor"),
        min_confidence=0.5,
    )

    assert [result.text for result in first.results] == [
        "one",
        "two",
        "three",
    ]
    assert second.results == first.results
    assert first.results[0].regions[0].to_dict() == {
        "region_id": 1,
        "text": "one",
        "confidence": 0.95,
        "bbox": {"x": 10, "y": 5, "width": 30, "height": 10},
    }
    assert first.usage == {
        "image_count": 3,
        "region_count": 3,
        "text_char_count": 11,
        "batch_size": 2,
        "batch_count": 2,
        "batch_sizes": [2, 1],
        "configured_batch_size": 2,
        "provider_max_batch_size": 2,
    }
    assert first.model.provider == "local.ocr"
    assert first.model.revision == "5.5.0"
    assert first.model.runtime == "tesseract-cli/5.5.0"
    assert len(calls) == 3
    assert calls[0][0] == (
        "fake-tesseract",
        "stdin",
        "stdout",
        "-l",
        "eng+kor",
        "--psm",
        "1",
        "tsv",
    )
    assert all(call[2] > 0 for call in calls)
    assert asyncio.run(provider.health()).status is HealthState.AVAILABLE


def test_provider_uses_non_orientation_page_segmentation_mode(
    tmp_path: Path,
) -> None:
    store = LocalArtifactStore(tmp_path, namespace="run-ocr")
    image = publish_image(store, "one", b"one")
    arguments = []

    def runner(args, payload, timeout_sec):
        arguments.append(args)
        return OCRCommandResult(0, tsv_for("one"), b"")

    _, service = make_service(store, runner=runner)

    service.recognize([image], detect_orientation=False)

    assert arguments[0][-3:] == ("--psm", "3", "tsv")
    assert arguments[0][6] == "3"


def test_missing_command_has_stable_health_model_and_failure(
    tmp_path: Path,
) -> None:
    store = LocalArtifactStore(tmp_path, namespace="run-ocr")
    image = publish_image(store, "one", b"one")
    provider, service = make_service(
        store,
        runner=lambda *_: pytest.fail("runner must not be called"),
        version_resolver=lambda command: None,
    )

    health = asyncio.run(provider.health())
    effective = asyncio.run(provider.effective_model())
    with pytest.raises(InferenceCallError) as exc_info:
        service.recognize([image])

    assert health.status is HealthState.UNAVAILABLE
    assert health.details["reason"] == "OCR_COMMAND_NOT_FOUND"
    assert effective is None
    assert exc_info.value.failure.code is InferenceErrorCode.MODEL_UNAVAILABLE
    assert exc_info.value.failure.details["reason"] == "OCR_COMMAND_NOT_FOUND"


def test_language_data_failure_is_normalized(
    tmp_path: Path,
) -> None:
    store = LocalArtifactStore(tmp_path, namespace="run-ocr")
    image = publish_image(store, "one", b"one")
    _, service = make_service(
        store,
        runner=lambda *_: OCRCommandResult(
            1,
            b"",
            b"Error opening data file kor.traineddata",
        ),
    )

    with pytest.raises(InferenceCallError) as exc_info:
        service.recognize([image], languages=("kor",))

    assert exc_info.value.failure.code is InferenceErrorCode.MODEL_UNAVAILABLE
    assert exc_info.value.failure.details == {
        "reason": "OCR_LANGUAGE_DATA_UNAVAILABLE",
        "languages": ["kor"],
    }


def test_provider_rejects_missing_and_corrupt_artifacts(
    tmp_path: Path,
) -> None:
    store = LocalArtifactStore(tmp_path, namespace="run-ocr")
    missing = publish_image(store, "missing", b"missing")
    corrupt = publish_image(store, "corrupt", b"original")
    (tmp_path / "frames" / "missing.jpg").unlink()
    (tmp_path / "frames" / "corrupt.jpg").write_bytes(b"changed")
    _, service = make_service(
        store,
        runner=lambda *_: pytest.fail("runner must not be called"),
    )

    with pytest.raises(InferenceCallError) as missing_error:
        service.recognize([missing])
    with pytest.raises(InferenceCallError) as corrupt_error:
        service.recognize([corrupt])

    assert (
        missing_error.value.failure.code
        is InferenceErrorCode.ARTIFACT_NOT_FOUND
    )
    assert (
        corrupt_error.value.failure.code
        is InferenceErrorCode.ARTIFACT_INTEGRITY_ERROR
    )


def test_service_rejects_out_of_bounds_provider_boxes(
    tmp_path: Path,
) -> None:
    store = LocalArtifactStore(tmp_path, namespace="run-ocr")
    image = publish_image(store, "one", b"one")
    _, service = make_service(
        store,
        runner=lambda *_: OCRCommandResult(
            0,
            tsv_for("outside", left=90, width=20),
            b"",
        ),
    )

    with pytest.raises(InferenceCallError) as exc_info:
        service.recognize([image])

    assert exc_info.value.failure.code is InferenceErrorCode.INFERENCE_FAILED
    assert "bbox exceeds image bounds" in exc_info.value.failure.message


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"languages": ()}, "languages"),
        ({"languages": ("en-US",)}, "languages"),
        ({"detect_orientation": 1}, "detect_orientation"),
        ({"min_confidence": -0.1}, "min_confidence"),
        ({"min_confidence": float("nan")}, "min_confidence"),
    ],
)
def test_service_rejects_invalid_options(
    tmp_path: Path,
    kwargs: dict[str, object],
    message: str,
) -> None:
    store = LocalArtifactStore(tmp_path, namespace="run-ocr")
    image = publish_image(store, "one", b"one")
    _, service = make_service(
        store,
        runner=lambda *_: pytest.fail("runner must not be called"),
    )

    with pytest.raises(ValueError, match=message):
        service.recognize([image], **kwargs)
