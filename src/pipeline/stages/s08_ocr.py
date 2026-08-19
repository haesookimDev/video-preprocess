"""8단계 보조 분기: 선택된 키프레임의 화면 문자를 OCR로 추출한다.

입력: 03_keyframes/keyframes.json, 08_captions/captions.json
출력: 08_ocr/ocr.json
"""

import re
import time

from .s08_captions import _image_media_type, _normalize_keyframes
from ..context import PipelineContext
from ..logging_setup import stage_logger
from ._reprocessing import (
    keyed_entries,
    provenance,
    selected_scene_ids,
    source_path,
)


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


def _merge_source_ocr(
    ctx: PipelineContext,
    keyframes: list[dict],
    selected_results: list[dict],
    selected_ids: tuple[int, ...],
) -> list[dict]:
    source_payload = ctx.load_json(source_path(ctx, "08_ocr/ocr.json"))
    source = keyed_entries(source_payload.get("results"), "source OCR results")
    selected = keyed_entries(selected_results, "selected OCR results")
    selected_set = set(selected_ids)
    combined = []
    for keyframe in keyframes:
        key = (keyframe["scene_id"], keyframe["keyframe_index"])
        origin = "selected-pass" if key[0] in selected_set else "source"
        entries = selected if origin == "selected-pass" else source
        entry = entries.get(key)
        if entry is None:
            if origin == "source":
                continue
            raise ValueError(f"selected OCR does not cover visual {key}")
        if entry.get("keyframe") != keyframe["path"]:
            raise ValueError("OCR keyframe path does not match overlay")
        combined.append({
            **entry,
            "keyframe_index": keyframe["keyframe_index"],
            "keyframe_count": keyframe["keyframe_count"],
            "timestamp_sec": keyframe["timestamp_sec"],
            "reprocessing": provenance(ctx, origin),
        })
    return combined


def run(ctx: PipelineContext) -> dict[str, object]:
    log = stage_logger(NAME)
    out_dir = ctx.stage_dir(NAME)
    keyframes = _normalize_keyframes(ctx.load_json(
        ctx.out_root / "03_keyframes" / "keyframes.json"
    )["keyframes"])
    reprocessing_scene_ids = selected_scene_ids(
        ctx,
        (keyframe["scene_id"] for keyframe in keyframes),
    )
    selected_set = set(reprocessing_scene_ids)
    if reprocessing_scene_ids and ctx.ocr_mode != "all":
        raise ValueError("visual-detail reprocessing requires ocr_mode=all")
    processing_keyframes = (
        [
            keyframe
            for keyframe in keyframes
            if keyframe["scene_id"] in selected_set
        ]
        if reprocessing_scene_ids
        else keyframes
    )

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
            processing_keyframes,
            caption_entries,
        )
    else:
        candidates = processing_keyframes
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
    if len(batch.results) != len(candidates):
        raise ValueError("OCR provider result count does not match input")
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

    selected_result_count = len(results)
    if reprocessing_scene_ids:
        results = _merge_source_ocr(
            ctx,
            keyframes,
            results,
            reprocessing_scene_ids,
        )

    text_frame_count = sum(bool(result["text"]) for result in results)
    region_count = sum(len(result["regions"]) for result in results)
    payload = {
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
    }
    if reprocessing_scene_ids:
        payload["reprocessing"] = {
            **provenance(ctx, "selected-pass"),
            "selected_scene_ids": list(reprocessing_scene_ids),
            "processed_result_count": selected_result_count,
            "reused_result_count": len(results) - selected_result_count,
        }
    ctx.save_json(out_dir / "ocr.json", payload)
    log.info(
        "OCR 완료: %d장, text %d장, region %d개 (%.1fs)",
        len(results),
        text_frame_count,
        region_count,
        time.monotonic() - started,
    )
    metrics = {
        "ocr_image_count": len(results),
        "ocr_text_frame_count": text_frame_count,
        "ocr_region_count": region_count,
    }
    if reprocessing_scene_ids:
        metrics.update({
            "processed_ocr_image_count": selected_result_count,
            "reused_ocr_image_count": len(results) - selected_result_count,
        })
    return metrics
