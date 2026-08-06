"""Errors raised by storage ports and adapters."""


class StorageError(RuntimeError):
    """Base class for artifact and manifest storage failures."""


class InvalidArtifactPathError(StorageError, ValueError):
    """An artifact path is absolute, unsafe, or reserved."""


class InvalidArtifactURIError(StorageError, ValueError):
    """An ArtifactRef URI cannot be handled by this store."""


class ArtifactNotFoundError(StorageError, FileNotFoundError):
    """A referenced artifact is not present in the backing store."""


class ArtifactIntegrityError(StorageError):
    """An artifact does not match its declared size or checksum."""


class UnsupportedChecksumError(StorageError, ValueError):
    """The adapter does not implement the requested checksum algorithm."""


class PendingArtifactError(StorageError):
    """A temporary artifact is unknown, stale, or already consumed."""


class ManifestFormatError(StorageError, ValueError):
    """A persisted manifest is not valid JSON or violates its contract."""


class LegacyArtifactFormatError(StorageError, ValueError):
    """A legacy JSON artifact cannot be decoded safely."""


class IncompleteRunError(StorageError):
    """A succeeded run references a missing or incomplete Stage attempt."""
