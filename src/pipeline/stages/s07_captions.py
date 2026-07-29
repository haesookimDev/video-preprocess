"""7단계: 경량 VLM(BLIP)으로 씬 키프레임 캡션을 생성한다.

- 이미지 대신 텍스트 캡션을 LLM에 전달하기 위한 "프레임의 텍스트화" 단계.
- BLIP base는 영어 캡션만 생성한다 (프로토타입 한정, 추후 한국어 VLM 교체 가능).

입력: 03_keyframes/keyframes.json
출력: 07_captions/captions.json
"""

import time

from PIL import Image

from ..context import PipelineContext
from ..logging_setup import stage_logger

NAME = "07_captions"
OUTPUT = "07_captions/captions.json"


def run(ctx: PipelineContext) -> dict:
    log = stage_logger(NAME)
    out_dir = ctx.stage_dir(NAME)

    keyframes = ctx.load_json(
        ctx.out_root / "03_keyframes" / "keyframes.json"
    )["keyframes"]

    log.info("캡셔닝 모델 로드: %s", ctx.caption_model)
    t0 = time.monotonic()
    # 로드가 느린 라이브러리라 단계 진입 시점에 임포트
    from transformers import BlipForConditionalGeneration, BlipProcessor

    processor = BlipProcessor.from_pretrained(ctx.caption_model)
    model = BlipForConditionalGeneration.from_pretrained(ctx.caption_model)
    log.debug("모델 로드 완료 (%.1fs)", time.monotonic() - t0)

    captions = []
    t_start = time.monotonic()
    for kf in keyframes:
        image_path = ctx.out_root / kf["path"]
        t_frame = time.monotonic()
        image = Image.open(image_path).convert("RGB")
        inputs = processor(images=image, return_tensors="pt")
        output_ids = model.generate(**inputs, max_new_tokens=40)
        caption = processor.decode(output_ids[0], skip_special_tokens=True)
        captions.append({
            "scene_id": kf["scene_id"],
            "timestamp_sec": kf["timestamp_sec"],
            "keyframe": kf["path"],
            "caption": caption,
        })
        log.info("씬 %02d 캡션 (%.1fs): %s",
                 kf["scene_id"], time.monotonic() - t_frame, caption)

    elapsed = time.monotonic() - t_start
    log.info("캡션 %d개 생성 완료 (%.1fs, 장당 평균 %.1fs)",
             len(captions), elapsed, elapsed / len(captions) if captions else 0)

    ctx.save_json(out_dir / "captions.json", {
        "model": ctx.caption_model,
        "captions": captions,
    })
    return {"caption_count": len(captions)}
