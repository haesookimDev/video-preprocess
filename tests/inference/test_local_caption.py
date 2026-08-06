"""Tests for the ArtifactRef-backed local caption provider."""

import asyncio
import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from video_preprocess.domain import (
    HealthState,
    InferenceErrorCode,
    InferenceTask,
)
from video_preprocess.inference import CaptionService, InferenceCallError
from video_preprocess.inference.gateway import InferenceGateway
from video_preprocess.inference.local import LocalCaptionProvider
from video_preprocess.storage import LocalArtifactStore


class FakeProcessor:
    def __init__(self) -> None:
        self.call_count = 0

    def __call__(self, *, images, return_tensors, padding):
        self.call_count += 1
        assert return_tensors == "pt"
        assert padding is True
        return {"pixel_values": list(images)}

    def batch_decode(self, sequences, *, skip_special_tokens):
        assert skip_special_tokens is True
        return list(sequences)


class FakeModel:
    def __init__(self) -> None:
        self.config = SimpleNamespace(_commit_hash="commit_caption_123")
        self.generate_count = 0

    def generate(self, *, pixel_values, max_new_tokens):
        self.generate_count += 1
        assert max_new_tokens == 40
        return [f"caption for {value}" for value in pixel_values]


def publish_image(
    store: LocalArtifactStore,
    name: str,
    content: bytes,
):
    pending = store.put(
        io.BytesIO(content),
        artifact_id=f"art_{name}",
        relative_path=f"frames/{name}.jpg",
        kind="image",
        media_type="image/jpeg",
    )
    return store.publish(pending)


def make_service(
    store: LocalArtifactStore,
    *,
    loader,
    image_loader=lambda stream: stream.read().decode("utf-8"),
    max_batch_size: int = 16,
):
    provider = LocalCaptionProvider(
        alias="caption.default",
        model_name="example/caption",
        artifact_store=store,
        revision=None,
        max_batch_size=max_batch_size,
        loader=loader,
        image_loader=image_loader,
    )
    service = CaptionService(
        InferenceGateway({"caption.default": provider}),
        alias="caption.default",
        model_name="example/caption",
        revision=provider.requested_revision,
    )
    return provider, service


def test_provider_batches_images_and_reuses_model_and_result(
    tmp_path: Path,
) -> None:
    store = LocalArtifactStore(tmp_path, namespace="run_123")
    images = [
        publish_image(store, "one", b"one"),
        publish_image(store, "two", b"two"),
    ]
    processor = FakeProcessor()
    model = FakeModel()
    load_count = 0

    def loader(model_name, revision, device):
        nonlocal load_count
        load_count += 1
        assert (model_name, revision, device) == (
            "example/caption",
            None,
            None,
        )
        return processor, model

    provider, service = make_service(store, loader=loader)

    first = service.caption(images)
    second = service.caption(images)

    assert first.captions == ("caption for one", "caption for two")
    assert second.captions == first.captions
    assert first.model.provider == "local.caption"
    assert first.model.revision == "commit_caption_123"
    assert first.usage["batch_size"] == 2
    assert load_count == 1
    assert processor.call_count == 1
    assert model.generate_count == 1
    assert provider.is_loaded


def test_provider_reuses_loaded_model_for_different_batch(
    tmp_path: Path,
) -> None:
    store = LocalArtifactStore(tmp_path, namespace="run_123")
    first_image = publish_image(store, "one", b"one")
    second_image = publish_image(store, "two", b"two")
    processor = FakeProcessor()
    model = FakeModel()
    provider, service = make_service(
        store,
        loader=lambda *_: (processor, model),
    )

    service.caption([first_image])
    service.caption([second_image])

    assert provider.is_loaded
    assert processor.call_count == 2
    assert model.generate_count == 2


def test_provider_rejects_missing_and_corrupt_artifacts(
    tmp_path: Path,
) -> None:
    store = LocalArtifactStore(tmp_path, namespace="run_123")
    missing = publish_image(store, "missing", b"missing")
    corrupt = publish_image(store, "corrupt", b"original")
    (tmp_path / "frames" / "missing.jpg").unlink()
    (tmp_path / "frames" / "corrupt.jpg").write_bytes(b"changed")
    _, service = make_service(
        store,
        loader=lambda *_: (FakeProcessor(), FakeModel()),
    )

    with pytest.raises(InferenceCallError) as missing_error:
        service.caption([missing])
    with pytest.raises(InferenceCallError) as corrupt_error:
        service.caption([corrupt])

    assert (
        missing_error.value.failure.code
        is InferenceErrorCode.ARTIFACT_NOT_FOUND
    )
    assert (
        corrupt_error.value.failure.code
        is InferenceErrorCode.ARTIFACT_INTEGRITY_ERROR
    )


def test_provider_normalizes_decode_and_model_load_failures(
    tmp_path: Path,
) -> None:
    store = LocalArtifactStore(tmp_path, namespace="run_123")
    image = publish_image(store, "one", b"one")
    _, decode_service = make_service(
        store,
        loader=lambda *_: (FakeProcessor(), FakeModel()),
        image_loader=lambda _: (_ for _ in ()).throw(ValueError("bad image")),
    )

    with pytest.raises(InferenceCallError) as decode_error:
        decode_service.caption([image])

    def fail_loader(*_):
        raise RuntimeError("model unavailable")

    _, load_service = make_service(store, loader=fail_loader)
    with pytest.raises(InferenceCallError) as load_error:
        load_service.caption([image])

    assert decode_error.value.failure.code is InferenceErrorCode.INVALID_REQUEST
    assert load_error.value.failure.code is InferenceErrorCode.MODEL_UNAVAILABLE


def test_gateway_rejects_caption_batch_before_loading_model(
    tmp_path: Path,
) -> None:
    store = LocalArtifactStore(tmp_path, namespace="run_123")
    images = [
        publish_image(store, "one", b"one"),
        publish_image(store, "two", b"two"),
    ]
    load_count = 0

    def loader(*_):
        nonlocal load_count
        load_count += 1
        return FakeProcessor(), FakeModel()

    _, service = make_service(
        store,
        loader=loader,
        max_batch_size=1,
    )

    with pytest.raises(InferenceCallError) as exc_info:
        service.caption(images)

    assert (
        exc_info.value.failure.code
        is InferenceErrorCode.UNSUPPORTED_CAPABILITY
    )
    assert load_count == 0


def test_provider_capabilities_health_and_warmup_are_lazy(
    tmp_path: Path,
) -> None:
    store = LocalArtifactStore(tmp_path, namespace="run_123")
    load_count = 0

    def loader(*_):
        nonlocal load_count
        load_count += 1
        return FakeProcessor(), FakeModel()

    provider, _ = make_service(store, loader=loader)

    capabilities = asyncio.run(provider.capabilities())
    health = asyncio.run(provider.health())

    assert capabilities.model_aliases == ("caption.default",)
    assert capabilities.tasks == (InferenceTask.IMAGE_CAPTIONING,)
    assert health.status is HealthState.AVAILABLE
    assert health.details["model_loaded"] is False
    assert load_count == 0

    asyncio.run(provider.warmup())

    assert load_count == 1
    assert provider.is_loaded
