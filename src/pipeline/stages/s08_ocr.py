"""8단계 보조 분기: 선택된 키프레임의 화면 문자를 OCR로 추출한다.

입력: 03_keyframes/keyframes.json, 08_captions/captions.json
출력: 08_ocr/ocr.json
"""

import re
import time

from .s08_captions import _image_media_type, _normalize_keyframes
from ..context import PipelineContext
from ..logging_setup import stage_logger


NAME = "08_ocr"
OUTPUT = "08_ocr/ocr.json"
OCR_POLICY = "deduplicated-keyframes-v1"
CAPTION_HINT_POLICY = "caption-keyword-hints-v1"
CAPTION_HINTS = (
    "subtitle",
    "presentation",
    "document",
    "writing",
    "words",
    "title",
    "text",
    "sign",
    "screen",
    "slide",
    "menu",
    "label",
)


def _skip(
    ctx: PipelineContext,
    out_dir,
    *,
    reason_code: str,
    reason: str,
    source_count: int,
) -> dict[str, object]:
    ctx.save_json(out_dir / "ocr.json", {
        "enabled": ctx.ocr_mode != "disabled",
        "executed": False,
        "reason_code": reason_code,
        "reason": reason,
        "model": ctx.ocr_model,
        "ocr_policy": OCR_POLICY,
        "trigger_policy": ctx.ocr_mode,
        "source_keyframe_count": source_count,
        "candidate_count": 0,
        "languages": list(ctx.ocr_languages),
        "detect_orientation": ctx.ocr_detect_orientation,
        "min_confidence": ctx.ocr_min_confidence,
        "results": [],
    })
    return {
        "ocr_image_count": 0,
        "ocr_text_frame_count": 0,
        "ocr_region_count": 0,
        "skipped": reason_code,
    }


def _caption_candidates(
    keyframes: list[dict],
    captions: list[dict],
) -> tuple[list[dict], dict[tuple[int, int], str]]:
    hints = {}
    for caption in captions:
        try:
            key = (
                int(caption["scene_id"]),
                int(caption.get("keyframe_index", 1)),
            )
            text = caption["caption"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("caption hint input is invalid") from exc
        if not isinstance(text, str):
            raise ValueError("caption hint text must be a string")
        lowered = text.lower()
        match = next(
            (
                hint
                for hint in CAPTION_HINTS
                if re.search(
                    rf"(?<![a-z0-9]){re.escape(hint)}s?(?![a-z0-9])",
                    lowered,
                )
            ),
            None,
        )
        if match is not None:
            hints[key] = match
    candidates = [
        keyframe
        for keyframe in keyframes
        if (keyframe["scene_id"], keyframe["keyframe_index"]) in hints
    ]
    return candidates, hints


def run(ctx: PipelineContext) -> dict[str, object]:
    log = stage_logger(NAME)
    out_dir = ctx.stage_dir(NAME)
    keyframes = _normalize_keyframes(ctx.load_json(
        ctx.out_root / "03_keyframes" / "keyframes.json"
    )["keyframes"])

    if ctx.ocr_mode == "disabled":
        log.info("OCR 비활성화 — 08_ocr 스킵")
        return _skip(
            ctx,
            out_dir,
            reason_code="OCR_DISABLED",
            reason="OCR is disabled by pipeline settings",
            source_count=len(keyframes),
        )
    if ctx.ocr_mode not in {"all", "caption-hints"}:
        raise ValueError("ocr_mode must be disabled, all, or caption-hints")
    if not keyframes:
        return _skip(
            ctx,
            out_dir,
            reason_code="NO_KEYFRAMES",
            reason="keyframe input is empty",
            source_count=0,
        )

    trigger_hints: dict[tuple[int, int], str] = {}
    if ctx.ocr_mode == "caption-hints":
        caption_entries = ctx.load_json(
            ctx.out_root / "08_captions" / "captions.json"
        )["captions"]
        candidates, trigger_hints = _caption_candidates(
            keyframes,
            caption_entries,
        )
    else:
        candidates = keyframes
    if not candidates:
        log.info("OCR trigger에 해당하는 키프레임 없음 — 08_ocr 스킵")
        return _skip(
            ctx,
            out_dir,
            reason_code="NO_OCR_CANDIDATES",
            reason="no keyframe matched the OCR trigger policy",
            source_count=len(keyframes),
        )

    if ctx.ocr_service is None or ctx.artifact_registrar is None:
        raise RuntimeError("OCR inference dependencies were not configured")
    image_refs = []
    for keyframe in candidates:
        artifact_id = f"ocr_keyframe_scene_{keyframe['scene_id']:03d}"
        if keyframe["keyframe_count"] > 1:
            artifact_id += f"_{keyframe['keyframe_index']:02d}"
        image_refs.append(ctx.artifact_registrar.register_file(
            keyframe["path"],
            artifact_id=artifact_id,
            kind="image",
            media_type=_image_media_type(keyframe["path"]),
            metadata={
                "scene_id": keyframe["scene_id"],
                "keyframe_index": keyframe["keyframe_index"],
                "keyframe_count": keyframe["keyframe_count"],
                "timestamp_sec": keyframe["timestamp_sec"],
                "stage": "03_keyframes",
            },
        ))

    log.info(
        "OCR provider 호출: ocr.default → %s (%d/%d장, mode=%s)",
        ctx.ocr_model,
        len(candidates),
        len(keyframes),
        ctx.ocr_mode,
    )
    started = time.monotonic()
    batch = ctx.ocr_service.recognize(
        image_refs,
        languages=ctx.ocr_languages,
        detect_orientation=ctx.ocr_detect_orientation,
        min_confidence=ctx.ocr_min_confidence,
        run_id=ctx.out_root.name,
        stage_run_id=NAME,
    )

    results = []
    for keyframe, result in zip(candidates, batch.results):
        key = (keyframe["scene_id"], keyframe["keyframe_index"])
        entry = {
            "scene_id": keyframe["scene_id"],
            "keyframe_index": keyframe["keyframe_index"],
            "keyframe_count": keyframe["keyframe_count"],
            "timestamp_sec": keyframe["timestamp_sec"],
            "keyframe": keyframe["path"],
            "text": result.text,
            "image_width": result.image_width,
            "image_height": result.image_height,
            "regions": [region.to_dict() for region in result.regions],
        }
        if key in trigger_hints:
            entry["trigger_hint"] = trigger_hints[key]
        results.append(entry)
        log.info(
            "씬 %02d OCR: %d개 region, %s",
            keyframe["scene_id"],
            len(result.regions),
            result.text or "(텍스트 없음)",
        )

    text_frame_count = sum(bool(result["text"]) for result in results)
    region_count = sum(len(result["regions"]) for result in results)
    ctx.save_json(out_dir / "ocr.json", {
        "enabled": True,
        "executed": True,
        "model": ctx.ocr_model,
        "provider": batch.model.provider,
        "revision": batch.model.revision,
        "runtime": batch.model.runtime,
        "ocr_policy": OCR_POLICY,
        "trigger_policy": ctx.ocr_mode,
        "trigger_hint_policy": (
            CAPTION_HINT_POLICY if ctx.ocr_mode == "caption-hints" else None
        ),
        "source_keyframe_count": len(keyframes),
        "candidate_count": len(candidates),
        "languages": list(ctx.ocr_languages),
        "detect_orientation": ctx.ocr_detect_orientation,
        "min_confidence": ctx.ocr_min_confidence,
        "usage": batch.usage,
        "timing": batch.timing,
        "results": results,
    })
    log.info(
        "OCR 완료: %d장, text %d장, region %d개 (%.1fs)",
        len(results),
        text_frame_count,
        region_count,
        time.monotonic() - started,
    )
    return {
        "ocr_image_count": len(results),
        "ocr_text_frame_count": text_frame_count,
        "ocr_region_count": region_count,
    }
