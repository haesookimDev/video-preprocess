"""Artifact-backed local OCR provider using the Tesseract CLI."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import math
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

from video_preprocess.domain import (
    ArtifactRef,
    EffectiveModel,
    HealthState,
    InferenceErrorCode,
    InferenceFailure,
    InferenceRequest,
    InferenceResponse,
    InferenceStatus,
    InferenceTask,
    ProviderCapabilities,
    ProviderHealth,
)
from video_preprocess.inference.gateway import InferenceGateway
from video_preprocess.inference.ocr import OCRService
from video_preprocess.storage import ArtifactStore
from video_preprocess.storage.errors import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
)


DEFAULT_OCR_MODEL = "tesseract"
DEFAULT_OCR_REVISION = "system"
DEFAULT_OCR_BATCH_SIZE = 4


@dataclass(frozen=True, slots=True)
class OCRCommandResult:
    """Process result kept small so tests need no real subprocess."""

    returncode: int
    stdout: bytes
    stderr: bytes


ProcessRunner = Callable[[tuple[str, ...], bytes, float], OCRCommandResult]
VersionResolver = Callable[[str], str | None]


def _default_process_runner(
    arguments: tuple[str, ...],
    payload: bytes,
    timeout_sec: float,
) -> OCRCommandResult:
    completed = subprocess.run(
        arguments,
        input=payload,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout_sec,
    )
    return OCRCommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _default_version_resolver(command: str) -> str | None:
    executable = shutil.which(command)
    if executable is None:
        return None
    try:
        completed = subprocess.run(
            (executable, "--version"),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    first_line = completed.stdout.decode(
        "utf-8",
        errors="replace",
    ).splitlines()
    if not first_line:
        return None
    parts = first_line[0].strip().split()
    if len(parts) < 2 or parts[0].lower() != "tesseract":
        return None
    return parts[1]


class LocalOCRProvider:
    """Run deterministic per-image OCR without exposing host paths."""

    PROVIDER_NAME = "local.ocr"
    INPUT_MEDIA_TYPES = ("image/jpeg", "image/png", "image/webp", "image/tiff")

    def __init__(
        self,
        *,
        alias: str,
        model_name: str,
        artifact_store: ArtifactStore,
        revision: str = DEFAULT_OCR_REVISION,
        command: str = DEFAULT_OCR_MODEL,
        max_batch_size: int = DEFAULT_OCR_BATCH_SIZE,
        max_artifact_bytes: int = 25 * 1024 * 1024,
        process_runner: ProcessRunner = _default_process_runner,
        version_resolver: VersionResolver = _default_version_resolver,
    ) -> None:
        for value, field_name in (
            (alias, "alias"),
            (model_name, "model_name"),
            (revision, "revision"),
            (command, "command"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        for value, field_name in (
            (max_batch_size, "max_batch_size"),
            (max_artifact_bytes, "max_artifact_bytes"),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 1
            ):
                raise ValueError(f"{field_name} must be at least 1")
        if not callable(process_runner) or not callable(version_resolver):
            raise TypeError("process_runner and version_resolver must be callable")
        self.alias = alias.strip()
        self.model_name = model_name.strip()
        self.requested_revision = revision.strip()
        self.command = command.strip()
        self.artifact_store = artifact_store
        self.max_batch_size = max_batch_size
        self.max_artifact_bytes = max_artifact_bytes
        self._process_runner = process_runner
        self._version_resolver = version_resolver
        self._version: str | None = None
        self._version_is_resolved = False
        self._version_lock = threading.Lock()
        self._inference_lock = threading.Lock()
        self._responses: dict[str, tuple[str, InferenceResponse]] = {}

    async def capabilities(self) -> ProviderCapabilities:
        effective = await self.effective_model()
        effective_models = {} if effective is None else {self.alias: effective}
        return ProviderCapabilities(
            provider=self.PROVIDER_NAME,
            tasks=[InferenceTask.OPTICAL_CHARACTER_RECOGNITION],
            model_aliases=[self.alias],
            input_media_types=self.INPUT_MEDIA_TYPES,
            features=[
                "artifact_batch",
                "ordered_results",
                "word_bounding_boxes",
                "word_confidence",
                "orientation_detection",
                "multiple_languages",
            ],
            max_batch_size=self.max_batch_size,
            max_artifact_bytes=self.max_artifact_bytes,
            supports_cancellation=False,
            supports_async_jobs=False,
            effective_models=effective_models,
        )

    async def health(self) -> ProviderHealth:
        version = await asyncio.to_thread(self._resolve_version)
        if version is None:
            return ProviderHealth(
                provider=self.PROVIDER_NAME,
                status=HealthState.UNAVAILABLE,
                details={
                    "reason": "OCR_COMMAND_NOT_FOUND",
                    "command": self.command,
                },
            )
        return ProviderHealth(
            provider=self.PROVIDER_NAME,
            status=HealthState.AVAILABLE,
            details={"command": self.command, "version": version},
        )

    async def effective_model(self) -> EffectiveModel | None:
        version = await asyncio.to_thread(self._resolve_version)
        if version is None:
            return None
        return EffectiveModel(
            provider=self.PROVIDER_NAME,
            name=self.model_name,
            revision=version,
            runtime=f"tesseract-cli/{version}",
        )

    async def warmup(self) -> None:
        version = await asyncio.to_thread(self._resolve_version)
        if version is None:
            raise RuntimeError(f"OCR command is unavailable: {self.command}")

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        return await asyncio.to_thread(self._infer_sync, request)

    async def cancel(self, request_id: str) -> None:
        return None

    def _infer_sync(self, request: InferenceRequest) -> InferenceResponse:
        with self._inference_lock:
            fingerprint = self._fingerprint(request)
            cached = self._responses.get(request.idempotency_key)
            if cached is not None:
                cached_fingerprint, cached_response = cached
                if cached_fingerprint != fingerprint:
                    return self._failure(
                        request,
                        InferenceErrorCode.INVALID_REQUEST,
                        "idempotency key was reused with different input",
                        details={"reason": "IDEMPOTENCY_KEY_CONFLICT"},
                    )
                return self._with_request_id(cached_response, request.request_id)

            validation_error = self._validate_request(request)
            if validation_error is not None:
                return validation_error
            version = self._resolve_version()
            if version is None:
                return self._failure(
                    request,
                    InferenceErrorCode.MODEL_UNAVAILABLE,
                    "OCR command is unavailable",
                    details={
                        "reason": "OCR_COMMAND_NOT_FOUND",
                        "command": self.command,
                    },
                )

            images = request.inputs["images"]
            languages = request.parameters["languages"]
            detect_orientation = request.parameters["detect_orientation"]
            min_confidence = request.parameters["min_confidence"]
            assert isinstance(images, list)
            assert isinstance(languages, list)
            assert isinstance(detect_orientation, bool)
            assert isinstance(min_confidence, (int, float))

            deadline = time.monotonic() + request.timeout_sec
            results = []
            inference_start = time.monotonic()
            for index, artifact in enumerate(images):
                assert isinstance(artifact, ArtifactRef)
                payload = self._load_image(request, artifact, index)
                if isinstance(payload, InferenceResponse):
                    return payload
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._failure(
                        request,
                        InferenceErrorCode.PROVIDER_TIMEOUT,
                        "OCR inference deadline elapsed between images",
                        retryable=True,
                    )
                try:
                    command_result = self._process_runner(
                        self._arguments(
                            tuple(languages),
                            detect_orientation=detect_orientation,
                        ),
                        payload,
                        remaining,
                    )
                except subprocess.TimeoutExpired:
                    return self._failure(
                        request,
                        InferenceErrorCode.PROVIDER_TIMEOUT,
                        "OCR process exceeded the request deadline",
                        retryable=True,
                    )
                except FileNotFoundError:
                    self._version = None
                    return self._failure(
                        request,
                        InferenceErrorCode.MODEL_UNAVAILABLE,
                        "OCR command disappeared during inference",
                        details={
                            "reason": "OCR_COMMAND_NOT_FOUND",
                            "command": self.command,
                        },
                    )
                except Exception as exc:
                    return self._failure(
                        request,
                        InferenceErrorCode.INFERENCE_FAILED,
                        "OCR process could not be started",
                        details={"error_type": type(exc).__name__},
                    )
                if not isinstance(command_result, OCRCommandResult):
                    return self._failure(
                        request,
                        InferenceErrorCode.PROVIDER_UNAVAILABLE,
                        "OCR process runner returned an invalid result",
                    )
                if command_result.returncode != 0:
                    return self._process_failure(
                        request,
                        command_result,
                        languages=tuple(languages),
                    )
                try:
                    result = self._parse_tsv(
                        command_result.stdout,
                        artifact_id=artifact.artifact_id,
                        min_confidence=float(min_confidence),
                    )
                except (
                    AttributeError,
                    TypeError,
                    UnicodeDecodeError,
                    ValueError,
                    csv.Error,
                ) as exc:
                    return self._failure(
                        request,
                        InferenceErrorCode.INFERENCE_FAILED,
                        "OCR process returned invalid TSV",
                        details={"error_type": type(exc).__name__},
                    )
                results.append(result)

            response = InferenceResponse(
                request_id=request.request_id,
                status=InferenceStatus.SUCCEEDED,
                outputs={"results": results},
                model=EffectiveModel(
                    provider=self.PROVIDER_NAME,
                    name=self.model_name,
                    revision=version,
                    runtime=f"tesseract-cli/{version}",
                ),
                usage={
                    "image_count": len(results),
                    "region_count": sum(
                        len(result["regions"]) for result in results
                    ),
                    "languages": list(languages),
                },
                timing={
                    "inference_sec": round(
                        time.monotonic() - inference_start,
                        6,
                    )
                },
            )
            self._cache_response(request, fingerprint, response)
            return response

    def _validate_request(
        self,
        request: InferenceRequest,
    ) -> InferenceResponse | None:
        if request.task is not InferenceTask.OPTICAL_CHARACTER_RECOGNITION:
            return self._failure(
                request,
                InferenceErrorCode.UNSUPPORTED_CAPABILITY,
                "local OCR provider only supports optical_character_recognition",
            )
        if (
            request.model.alias != self.alias
            or request.model.name != self.model_name
            or request.model.revision != self.requested_revision
        ):
            return self._failure(
                request,
                InferenceErrorCode.UNSUPPORTED_CAPABILITY,
                "requested model does not match provider binding",
            )
        if set(request.inputs) != {"images"}:
            return self._failure(
                request,
                InferenceErrorCode.INVALID_REQUEST,
                "OCR inputs must contain only images",
            )
        images = request.inputs.get("images")
        if not isinstance(images, list) or not images:
            return self._failure(
                request,
                InferenceErrorCode.INVALID_REQUEST,
                "inputs.images must be a non-empty ArtifactRef array",
            )
        if len(images) > self.max_batch_size:
            return self._failure(
                request,
                InferenceErrorCode.INVALID_REQUEST,
                "OCR batch exceeds provider maximum",
                details={"max_batch_size": self.max_batch_size},
            )
        for index, image in enumerate(images):
            if not isinstance(image, ArtifactRef):
                return self._failure(
                    request,
                    InferenceErrorCode.INVALID_REQUEST,
                    f"inputs.images[{index}] must be an ArtifactRef",
                )
            if image.media_type not in self.INPUT_MEDIA_TYPES:
                return self._failure(
                    request,
                    InferenceErrorCode.INVALID_REQUEST,
                    f"inputs.images[{index}] has an unsupported media type",
                    details={"media_type": image.media_type},
                )
        if set(request.parameters) != {
            "languages",
            "detect_orientation",
            "min_confidence",
        }:
            return self._failure(
                request,
                InferenceErrorCode.INVALID_REQUEST,
                "OCR parameters do not match the v1 contract",
            )
        languages = request.parameters.get("languages")
        if (
            not isinstance(languages, list)
            or not languages
            or any(
                not isinstance(language, str) or not language
                for language in languages
            )
        ):
            return self._failure(
                request,
                InferenceErrorCode.INVALID_REQUEST,
                "parameters.languages must be a non-empty string array",
            )
        if not isinstance(
            request.parameters.get("detect_orientation"),
            bool,
        ):
            return self._failure(
                request,
                InferenceErrorCode.INVALID_REQUEST,
                "parameters.detect_orientation must be a boolean",
            )
        confidence = request.parameters.get("min_confidence")
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(float(confidence))
            or not 0 <= float(confidence) <= 1
        ):
            return self._failure(
                request,
                InferenceErrorCode.INVALID_REQUEST,
                "parameters.min_confidence must be between 0 and 1",
            )
        return None

    def _load_image(
        self,
        request: InferenceRequest,
        artifact: ArtifactRef,
        index: int,
    ) -> bytes | InferenceResponse:
        try:
            verification = self.artifact_store.verify(artifact)
            if not verification.exists:
                return self._failure(
                    request,
                    InferenceErrorCode.ARTIFACT_NOT_FOUND,
                    f"OCR input artifact is missing: images[{index}]",
                )
            if not verification.ok:
                return self._failure(
                    request,
                    InferenceErrorCode.ARTIFACT_INTEGRITY_ERROR,
                    f"OCR input artifact failed verification: images[{index}]",
                )
            with self.artifact_store.open(artifact) as stream:
                return stream.read()
        except ArtifactNotFoundError:
            return self._failure(
                request,
                InferenceErrorCode.ARTIFACT_NOT_FOUND,
                f"OCR input artifact is missing: images[{index}]",
            )
        except ArtifactIntegrityError:
            return self._failure(
                request,
                InferenceErrorCode.ARTIFACT_INTEGRITY_ERROR,
                f"OCR input artifact failed verification: images[{index}]",
            )
        except Exception as exc:
            return self._failure(
                request,
                InferenceErrorCode.INVALID_REQUEST,
                f"OCR input image could not be read: images[{index}]",
                details={"error_type": type(exc).__name__},
            )

    def _resolve_version(self) -> str | None:
        if self._version_is_resolved:
            return self._version
        with self._version_lock:
            if not self._version_is_resolved:
                resolved = self._version_resolver(self.command)
                if resolved is not None and (
                    not isinstance(resolved, str) or not resolved.strip()
                ):
                    raise ValueError(
                        "version_resolver must return non-empty text or None"
                    )
                self._version = None if resolved is None else resolved.strip()
                self._version_is_resolved = True
        return self._version

    def _arguments(
        self,
        languages: tuple[str, ...],
        *,
        detect_orientation: bool,
    ) -> tuple[str, ...]:
        return (
            self.command,
            "stdin",
            "stdout",
            "-l",
            "+".join(languages),
            "--psm",
            "1" if detect_orientation else "3",
            "tsv",
        )

    @staticmethod
    def _parse_tsv(
        payload: bytes,
        *,
        artifact_id: str,
        min_confidence: float,
    ) -> dict[str, object]:
        text = payload.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text), delimiter="\t")
        required = {
            "level",
            "block_num",
            "par_num",
            "line_num",
            "left",
            "top",
            "width",
            "height",
            "conf",
            "text",
        }
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise ValueError("TSV header is incomplete")
        rows = list(reader)
        page = next((row for row in rows if row["level"] == "1"), None)
        if page is None:
            raise ValueError("TSV page dimensions are missing")
        image_width = int(page["width"])
        image_height = int(page["height"])
        if image_width < 1 or image_height < 1:
            raise ValueError("TSV page dimensions are invalid")

        regions = []
        lines: dict[tuple[str, str, str], list[str]] = {}
        for row in rows:
            word = row["text"].strip()
            if row["level"] != "5" or not word:
                continue
            confidence = float(row["conf"])
            if not math.isfinite(confidence) or confidence < 0:
                continue
            normalized_confidence = confidence / 100.0
            if normalized_confidence < min_confidence:
                continue
            x = int(row["left"])
            y = int(row["top"])
            width = int(row["width"])
            height = int(row["height"])
            if min(x, y) < 0 or width < 1 or height < 1:
                raise ValueError("TSV word box is invalid")
            regions.append({
                "region_id": len(regions) + 1,
                "text": word,
                "confidence": round(normalized_confidence, 6),
                "bbox": {
                    "x": x,
                    "y": y,
                    "width": width,
                    "height": height,
                },
            })
            line_key = (
                row["block_num"],
                row["par_num"],
                row["line_num"],
            )
            lines.setdefault(line_key, []).append(word)
        full_text = "\n".join(" ".join(words) for words in lines.values())
        return {
            "artifact_id": artifact_id,
            "text": full_text,
            "image_width": image_width,
            "image_height": image_height,
            "regions": regions,
        }

    def _process_failure(
        self,
        request: InferenceRequest,
        result: OCRCommandResult,
        *,
        languages: tuple[str, ...],
    ) -> InferenceResponse:
        stderr = result.stderr.decode("utf-8", errors="replace").lower()
        if "traineddata" in stderr or "failed loading language" in stderr:
            return self._failure(
                request,
                InferenceErrorCode.MODEL_UNAVAILABLE,
                "OCR language data is unavailable",
                details={
                    "reason": "OCR_LANGUAGE_DATA_UNAVAILABLE",
                    "languages": list(languages),
                },
            )
        return self._failure(
            request,
            InferenceErrorCode.INFERENCE_FAILED,
            "OCR process failed",
            details={"exit_code": result.returncode},
        )

    @staticmethod
    def _fingerprint(request: InferenceRequest) -> str:
        payload = {
            "task": request.task.value,
            "model": request.model.to_dict(),
            "inputs": request.to_dict()["inputs"],
            "parameters": dict(request.parameters),
        }
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def _cache_response(
        self,
        request: InferenceRequest,
        fingerprint: str,
        response: InferenceResponse,
    ) -> None:
        if len(self._responses) >= 256:
            self._responses.pop(next(iter(self._responses)))
        self._responses[request.idempotency_key] = (fingerprint, response)

    @staticmethod
    def _with_request_id(
        response: InferenceResponse,
        request_id: str,
    ) -> InferenceResponse:
        error = response.error
        if error is not None:
            error = replace(error, request_id=request_id)
        return replace(response, request_id=request_id, error=error)

    @staticmethod
    def _failure(
        request: InferenceRequest,
        code: InferenceErrorCode,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, object] | None = None,
    ) -> InferenceResponse:
        return InferenceResponse(
            request_id=request.request_id,
            status=InferenceStatus.FAILED,
            error=InferenceFailure(
                code=code,
                message=message,
                retryable=retryable,
                details={} if details is None else details,
                request_id=request.request_id,
            ),
        )


def create_local_ocr_service(
    artifact_store: ArtifactStore,
    *,
    alias: str = "ocr.default",
    model_name: str = DEFAULT_OCR_MODEL,
    revision: str = DEFAULT_OCR_REVISION,
    command: str = DEFAULT_OCR_MODEL,
    max_batch_size: int = DEFAULT_OCR_BATCH_SIZE,
) -> OCRService:
    """Create an OCR service backed by one reusable local provider."""

    provider = LocalOCRProvider(
        alias=alias,
        model_name=model_name,
        revision=revision,
        command=command,
        artifact_store=artifact_store,
        max_batch_size=max_batch_size,
    )
    return OCRService(
        InferenceGateway({alias: provider}),
        alias=alias,
        model_name=model_name,
        revision=revision,
        batch_size=max_batch_size,
    )
