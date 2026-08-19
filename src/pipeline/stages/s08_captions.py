"""8단계: 경량 VLM(BLIP)으로 씬 키프레임 캡션을 생성한다.

- 이미지 대신 텍스트 캡션을 LLM에 전달하기 위한 "프레임의 텍스트화" 단계.
- BLIP base는 영어 캡션만 생성한다 (프로토타입 한정, 추후 한국어 VLM 교체 가능).

입력: 03_keyframes/keyframes.json
출력: 08_captions/captions.json
"""

import time
from collections import Counter
from pathlib import PurePosixPath

from ..context import PipelineContext
from ..logging_setup import stage_logger

NAME = "08_captions"
OUTPUT = "08_captions/captions.json"
CAPTION_POLICY = "per-keyframe-scene-group-v1"


def _image_media_type(relative_path: str) -> str:
    suffix = PurePosixPath(relative_path).suffix.lower()
    media_types = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }
    try:
        return media_types[suffix]
    except KeyError as exc:
        raise ValueError(
            f"지원하지 않는 키프레임 형식입니다: {relative_path}"
        ) from exc


def _normalize_keyframes(keyframes: list[dict]) -> list[dict]:
    """Add and validate one-based indices for legacy and adaptive inputs."""

    counts = Counter(int(keyframe["scene_id"]) for keyframe in keyframes)
    positions: Counter[int] = Counter()
    normalized = []
    for keyframe in keyframes:
        scene_id = int(keyframe["scene_id"])
        positions[scene_id] += 1
        index = positions[scene_id]
        count = counts[scene_id]
        supplied_index = keyframe.get("keyframe_index")
        supplied_count = keyframe.get("keyframe_count")
        if supplied_index is not None and supplied_index != index:
            raise ValueError(
                f"scene {scene_id} keyframe_index must follow input order"
            )
        if supplied_count is not None and supplied_count != count:
            raise ValueError(
                f"scene {scene_id} keyframe_count does not match entries"
            )
        normalized.append({
            **keyframe,
            "scene_id": scene_id,
            "keyframe_index": index,
            "keyframe_count": count,
        })
    return normalized


def _scene_caption_groups(captions: list[dict]) -> list[dict]:
    """Return ordered per-scene groups while retaining the legacy flat list."""

    grouped: dict[int, list[dict]] = {}
    for caption in captions:
        grouped.setdefault(caption["scene_id"], []).append(caption)
    return [
        {
            "scene_id": scene_id,
            "caption_count": len(entries),
            "captions": entries,
        }
        for scene_id, entries in grouped.items()
    ]


def run(ctx: PipelineContext) -> dict:
    log = stage_logger(NAME)
    out_dir = ctx.stage_dir(NAME)

    keyframes = _normalize_keyframes(ctx.load_json(
        ctx.out_root / "03_keyframes" / "keyframes.json"
    )["keyframes"])

    if ctx.caption_service is None or ctx.artifact_registrar is None:
        raise RuntimeError(
            "caption inference dependencies were not configured"
        )

    image_refs = []
    for kf in keyframes:
        artifact_id = f"keyframe_scene_{int(kf['scene_id']):03d}"
        if kf["keyframe_count"] > 1:
            artifact_id += f"_{int(kf['keyframe_index']):02d}"
        image_refs.append(
            ctx.artifact_registrar.register_file(
                kf["path"],
                artifact_id=artifact_id,
                kind="image",
                media_type=_image_media_type(kf["path"]),
                metadata={
                    "scene_id": kf["scene_id"],
                    "keyframe_index": kf["keyframe_index"],
                    "keyframe_count": kf["keyframe_count"],
                    "timestamp_sec": kf["timestamp_sec"],
                    "stage": "03_keyframes",
                },
            )
        )

    if not image_refs:
        ctx.save_json(
            out_dir / "captions.json",
            {
                "model": ctx.caption_model,
                "caption_policy": CAPTION_POLICY,
                "scene_count": 0,
                "captions": [],
                "scene_captions": [],
            },
        )
        return {"caption_count": 0, "caption_batch_count": 0}

    log.info(
        "캡션 provider 호출: caption.default → %s (%d장)",
        ctx.caption_model,
        len(image_refs),
    )
    t_start = time.monotonic()
    batch = ctx.caption_service.caption(
        image_refs,
        max_new_tokens=40,
        run_id=ctx.out_root.name,
        stage_run_id=NAME,
    )

    captions = []
    for kf, caption in zip(keyframes, batch.captions):
        captions.append({
            "scene_id": kf["scene_id"],
            "keyframe_index": kf["keyframe_index"],
            "keyframe_count": kf["keyframe_count"],
            "timestamp_sec": kf["timestamp_sec"],
            "keyframe": kf["path"],
            "caption": caption,
        })
        log.info("씬 %02d 캡션: %s", kf["scene_id"], caption)

    elapsed = time.monotonic() - t_start
    log.info(
        "캡션 %d개 생성 완료 (%.1fs, 장당 평균 %.1fs, "
        "batch=%s, device=%s)",
        len(captions),
        elapsed,
        elapsed / len(captions),
        batch.usage.get("batch_sizes", [len(captions)]),
        batch.usage.get("device", "provider_default"),
    )
    log.debug(
        "실제 캡션 모델: provider=%s model=%s revision=%s runtime=%s",
        batch.model.provider,
        batch.model.name,
        batch.model.revision,
        batch.model.runtime,
    )

    ctx.save_json(out_dir / "captions.json", {
        "model": ctx.caption_model,
        "provider": batch.model.provider,
        "revision": batch.model.revision,
        "runtime": batch.model.runtime,
        "usage": batch.usage,
        "timing": batch.timing,
        "caption_policy": CAPTION_POLICY,
        "scene_count": len({caption["scene_id"] for caption in captions}),
        "captions": captions,
        "scene_captions": _scene_caption_groups(captions),
    })
    return {
        "caption_count": len(captions),
        "caption_batch_count": batch.usage.get("batch_count", 1),
    }
