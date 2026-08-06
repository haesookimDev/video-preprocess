"""Errors raised while validating versioned domain contracts."""


class ContractValidationError(ValueError):
    """A contract field does not satisfy the public schema."""

    def __init__(self, field: str, message: str) -> None:
        self.field = field
        self.message = message
        super().__init__(f"{field}: {message}")


class UnsupportedSchemaVersion(ContractValidationError):
    """A serialized contract uses an unsupported schema version."""

    def __init__(self, version: str, supported: str) -> None:
        self.version = version
        self.supported = supported
        super().__init__(
            "schema_version",
            f"unsupported version {version!r}; supported version is {supported!r}",
        )

