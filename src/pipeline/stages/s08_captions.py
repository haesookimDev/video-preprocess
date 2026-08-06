"""8단계: 경량 VLM(BLIP)으로 씬 키프레임 캡션을 생성한다.

- 이미지 대신 텍스트 캡션을 LLM에 전달하기 위한 "프레임의 텍스트화" 단계.
- BLIP base는 영어 캡션만 생성한다 (프로토타입 한정, 추후 한국어 VLM 교체 가능).

입력: 03_keyframes/keyframes.json
출력: 08_captions/captions.json
"""

import time
from pathlib import PurePosixPath

from ..context import PipelineContext
from ..logging_setup import stage_logger

NAME = "08_captions"
OUTPUT = "08_captions/captions.json"


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


def run(ctx: PipelineContext) -> dict:
    log = stage_logger(NAME)
    out_dir = ctx.stage_dir(NAME)

    keyframes = ctx.load_json(
        ctx.out_root / "03_keyframes" / "keyframes.json"
    )["keyframes"]

    if ctx.caption_service is None or ctx.artifact_registrar is None:
        raise RuntimeError(
            "caption inference dependencies were not configured"
        )

    image_refs = []
    for kf in keyframes:
        image_refs.append(
            ctx.artifact_registrar.register_file(
                kf["path"],
                artifact_id=(
                    f"keyframe_scene_{int(kf['scene_id']):03d}"
                ),
                kind="image",
                media_type=_image_media_type(kf["path"]),
                metadata={
                    "scene_id": kf["scene_id"],
                    "timestamp_sec": kf["timestamp_sec"],
                    "stage": "03_keyframes",
                },
            )
        )

    if not image_refs:
        ctx.save_json(
            out_dir / "captions.json",
            {"model": ctx.caption_model, "captions": []},
        )
        return {"caption_count": 0}

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
            "timestamp_sec": kf["timestamp_sec"],
            "keyframe": kf["path"],
            "caption": caption,
        })
        log.info("씬 %02d 캡션: %s", kf["scene_id"], caption)

    elapsed = time.monotonic() - t_start
    log.info(
        "캡션 %d개 생성 완료 (%.1fs, 장당 평균 %.1fs)",
        len(captions),
        elapsed,
        elapsed / len(captions),
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
        "captions": captions,
    })
    return {"caption_count": len(captions)}
