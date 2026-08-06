"""Filesystem-backed Artifact Store preserving legacy output paths."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import uuid
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import BinaryIO
from urllib.parse import quote, unquote, urlsplit

from video_preprocess.domain import ArtifactRef, Checksum
from video_preprocess.domain._validation import JSONValue, normalize_json_object

from .artifacts import ArtifactVerification, PendingArtifact
from .errors import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    InvalidArtifactPathError,
    InvalidArtifactURIError,
    PendingArtifactError,
    UnsupportedChecksumError,
)


_NAMESPACE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_RESERVED_TOP_LEVEL = {"_manifests", "_pending"}
_COPY_CHUNK_SIZE = 1024 * 1024


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _normalize_relative_path(value: object) -> PurePosixPath:
    text = _require_text(value, "relative_path")
    if "\\" in text:
        raise InvalidArtifactPathError(
            "relative_path must use POSIX '/' separators"
        )
    relative = PurePosixPath(text)
    if relative.is_absolute() or not relative.parts:
        raise InvalidArtifactPathError("relative_path must be relative")
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise InvalidArtifactPathError(
            "relative_path must not contain '.' or '..' segments"
        )
    if relative.parts[0] in _RESERVED_TOP_LEVEL:
        raise InvalidArtifactPathError(
            f"top-level path {relative.parts[0]!r} is reserved"
        )
    return relative


def _sha256_stream(stream: BinaryIO, destination: BinaryIO) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = stream.read(_COPY_CHUNK_SIZE)
        if not isinstance(chunk, (bytes, bytearray)):
            raise TypeError("artifact stream must return bytes")
        if not chunk:
            break
        destination.write(chunk)
        digest.update(chunk)
        size += len(chunk)
    return size, digest.hexdigest()


def _inspect_file(path: Path) -> tuple[int, Checksum]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_COPY_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return size, Checksum("sha256", digest.hexdigest())


class LocalArtifactStore:
    """Maps one safe artifact namespace onto a local output directory."""

    def __init__(self, root: Path, *, namespace: str) -> None:
        namespace = _require_text(namespace, "namespace")
        if not _NAMESPACE_PATTERN.fullmatch(namespace):
            raise ValueError(
                "namespace must contain only letters, digits, '.', '_', or '-'"
            )
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.namespace = namespace
        self._pending_dir = self.root / "_pending"
        if self._pending_dir.is_symlink():
            raise InvalidArtifactPathError(
                "reserved _pending directory must not be a symbolic link"
            )
        self._pending_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._pending_dir.resolve().relative_to(self.root)
        except ValueError as exc:
            raise InvalidArtifactPathError(
                "reserved _pending directory escapes the artifact root"
            ) from exc
        self._pending: dict[str, PendingArtifact] = {}

    def put(
        self,
        stream: BinaryIO,
        *,
        artifact_id: str,
        relative_path: str,
        kind: str,
        media_type: str,
        metadata: Mapping[str, JSONValue] | None = None,
    ) -> PendingArtifact:
        """Copy bytes to a private temporary file without publishing them."""

        artifact_id = _require_text(artifact_id, "artifact_id")
        relative = _normalize_relative_path(relative_path)
        kind = _require_text(kind, "kind")
        media_type = _require_text(media_type, "media_type")
        normalized_metadata = normalize_json_object(
            {} if metadata is None else metadata,
            "metadata",
        )

        token = uuid.uuid4().hex
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self._pending_dir,
            prefix=f"{token}-",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as destination:
                size, digest = _sha256_stream(stream, destination)
                destination.flush()
                os.fsync(destination.fileno())
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise

        pending = PendingArtifact(
            token=token,
            artifact_id=artifact_id,
            relative_path=relative.as_posix(),
            kind=kind,
            media_type=media_type,
            size_bytes=size,
            checksum=Checksum("sha256", digest),
            metadata=normalized_metadata,
        )
        self._pending[token] = pending
        return pending

    def publish(self, pending: PendingArtifact) -> ArtifactRef:
        """Atomically move a known pending artifact into its public path."""

        expected = self._pending.get(pending.token)
        if expected is None or expected != pending:
            raise PendingArtifactError(
                f"unknown or modified pending artifact: {pending.token}"
            )
        temporary_path = self._pending_path(pending.token)
        if temporary_path is None:
            raise PendingArtifactError(
                f"pending bytes are missing: {pending.token}"
            )
        relative = _normalize_relative_path(pending.relative_path)
        target = self._target_path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temporary_path, target)
        del self._pending[pending.token]
        return self._build_ref(
            artifact_id=pending.artifact_id,
            relative=relative,
            kind=pending.kind,
            media_type=pending.media_type,
            size_bytes=pending.size_bytes,
            checksum=pending.checksum,
            metadata=pending.metadata,
        )

    def discard(self, pending: PendingArtifact) -> None:
        """Remove unpublished bytes; published artifacts are never deleted."""

        expected = self._pending.get(pending.token)
        if expected is None or expected != pending:
            raise PendingArtifactError(
                f"unknown or modified pending artifact: {pending.token}"
            )
        temporary_path = self._pending_path(pending.token)
        if temporary_path is not None:
            temporary_path.unlink()
        del self._pending[pending.token]

    def register_existing(
        self,
        relative_path: str,
        *,
        artifact_id: str,
        kind: str,
        media_type: str,
        metadata: Mapping[str, JSONValue] | None = None,
    ) -> ArtifactRef:
        """Create a verified reference for a legacy file already under root."""

        relative = _normalize_relative_path(relative_path)
        target = self._target_path(relative)
        if not target.is_file():
            raise ArtifactNotFoundError(str(target))
        try:
            size_bytes, checksum = _inspect_file(target)
        except FileNotFoundError as exc:
            raise ArtifactNotFoundError(str(target)) from exc
        return self._build_ref(
            artifact_id=_require_text(artifact_id, "artifact_id"),
            relative=relative,
            kind=_require_text(kind, "kind"),
            media_type=_require_text(media_type, "media_type"),
            size_bytes=size_bytes,
            checksum=checksum,
            metadata=normalize_json_object(
                {} if metadata is None else metadata,
                "metadata",
            ),
        )

    def open(self, artifact_ref: ArtifactRef) -> BinaryIO:
        path = self._path_from_ref(artifact_ref)
        try:
            return path.open("rb")
        except FileNotFoundError as exc:
            raise ArtifactNotFoundError(artifact_ref.uri) from exc

    def materialize(
        self,
        artifact_ref: ArtifactRef,
        workspace: Path,
    ) -> Path:
        source = self._path_from_ref(artifact_ref)
        if not source.is_file():
            raise ArtifactNotFoundError(artifact_ref.uri)
        workspace = Path(workspace).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        target = workspace / source.name
        if target.resolve() == source.resolve():
            return source

        descriptor, temporary_name = tempfile.mkstemp(
            dir=workspace,
            prefix=f".{source.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            with source.open("rb") as source_handle:
                with os.fdopen(descriptor, "wb") as destination:
                    size_bytes, digest = _sha256_stream(
                        source_handle, destination
                    )
                    destination.flush()
                    os.fsync(destination.fileno())
            actual_checksum = Checksum("sha256", digest)
            if (
                size_bytes != artifact_ref.size_bytes
                or actual_checksum != artifact_ref.checksum
            ):
                raise ArtifactIntegrityError(
                    f"artifact failed verification: {artifact_ref.uri}"
                )
            os.replace(temporary_path, target)
        except FileNotFoundError as exc:
            temporary_path.unlink(missing_ok=True)
            raise ArtifactNotFoundError(artifact_ref.uri) from exc
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise
        return target

    def exists(self, artifact_ref: ArtifactRef) -> bool:
        return self._path_from_ref(artifact_ref).is_file()

    def verify(self, artifact_ref: ArtifactRef) -> ArtifactVerification:
        if artifact_ref.checksum.algorithm != "sha256":
            raise UnsupportedChecksumError(
                f"unsupported checksum: {artifact_ref.checksum.algorithm}"
            )
        path = self._path_from_ref(artifact_ref)
        if not path.is_file():
            return ArtifactVerification(
                exists=False,
                expected_size_bytes=artifact_ref.size_bytes,
                actual_size_bytes=None,
                expected_checksum=artifact_ref.checksum,
                actual_checksum=None,
            )
        try:
            actual_size, actual_checksum = _inspect_file(path)
        except FileNotFoundError:
            return ArtifactVerification(
                exists=False,
                expected_size_bytes=artifact_ref.size_bytes,
                actual_size_bytes=None,
                expected_checksum=artifact_ref.checksum,
                actual_checksum=None,
            )
        return ArtifactVerification(
            exists=True,
            expected_size_bytes=artifact_ref.size_bytes,
            actual_size_bytes=actual_size,
            expected_checksum=artifact_ref.checksum,
            actual_checksum=actual_checksum,
        )

    def _pending_path(self, token: str) -> Path | None:
        matches = list(self._pending_dir.glob(f"{token}-*.tmp"))
        if len(matches) != 1:
            return None
        return matches[0]

    def _target_path(self, relative: PurePosixPath) -> Path:
        target = self.root.joinpath(*relative.parts)
        resolved = target.resolve(strict=False)
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise InvalidArtifactPathError(
                "relative_path escapes the artifact root"
            ) from exc
        return target

    def _build_ref(
        self,
        *,
        artifact_id: str,
        relative: PurePosixPath,
        kind: str,
        media_type: str,
        size_bytes: int,
        checksum: Checksum,
        metadata: Mapping[str, JSONValue],
    ) -> ArtifactRef:
        encoded_path = "/".join(
            quote(part, safe="-._~") for part in relative.parts
        )
        return ArtifactRef(
            artifact_id=artifact_id,
            kind=kind,
            uri=f"artifact://{self.namespace}/{encoded_path}",
            media_type=media_type,
            size_bytes=size_bytes,
            checksum=checksum,
            metadata=metadata,
        )

    def _path_from_ref(self, artifact_ref: ArtifactRef) -> Path:
        if not isinstance(artifact_ref, ArtifactRef):
            raise TypeError("artifact_ref must be an ArtifactRef")
        parsed = urlsplit(artifact_ref.uri)
        if (
            parsed.scheme != "artifact"
            or parsed.netloc != self.namespace
            or parsed.query
            or parsed.fragment
        ):
            raise InvalidArtifactURIError(
                f"artifact URI is not in namespace {self.namespace!r}"
            )
        raw_parts = parsed.path.removeprefix("/").split("/")
        if not raw_parts or any(not part for part in raw_parts):
            raise InvalidArtifactURIError("artifact URI path is empty")
        decoded_parts = []
        for raw_part in raw_parts:
            decoded = unquote(raw_part)
            if "/" in decoded or "\\" in decoded:
                raise InvalidArtifactURIError(
                    "artifact URI contains an encoded path separator"
                )
            if quote(decoded, safe="-._~") != raw_part:
                raise InvalidArtifactURIError("artifact URI is not canonical")
            decoded_parts.append(decoded)
        try:
            relative = _normalize_relative_path("/".join(decoded_parts))
        except InvalidArtifactPathError as exc:
            raise InvalidArtifactURIError(str(exc)) from exc
        return self._target_path(relative)
