"""Validation and normalization helpers for domain contracts."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from .errors import ContractValidationError, UnsupportedSchemaVersion


SCHEMA_VERSION = "1"
JSONScalar = str | int | float | bool | None
JSONValue = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]


def require_schema_version(version: object) -> str:
    if not isinstance(version, str):
        raise ContractValidationError("schema_version", "must be a string")
    if version != SCHEMA_VERSION:
        raise UnsupportedSchemaVersion(version, SCHEMA_VERSION)
    return version


def require_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractValidationError(field, "must be a non-empty string")
    return value


def optional_string(value: object, field: str) -> str | None:
    if value is None:
        return None
    return require_string(value, field)


def require_integer(value: object, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractValidationError(field, "must be an integer")
    if value < minimum:
        raise ContractValidationError(field, f"must be at least {minimum}")
    return value


def require_number(value: object, field: str, *, minimum: float = 0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractValidationError(field, "must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ContractValidationError(field, "must be finite")
    if number < minimum:
        raise ContractValidationError(field, f"must be at least {minimum}")
    return number


def require_mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractValidationError(field, "must be an object")
    for key in value:
        if not isinstance(key, str):
            raise ContractValidationError(field, "must use string keys")
    return value


def normalize_json(value: object, field: str) -> JSONValue:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractValidationError(field, "must not contain NaN or infinity")
        return value
    if isinstance(value, Mapping):
        normalized = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ContractValidationError(field, "must use string keys")
            normalized[key] = normalize_json(item, f"{field}.{key}")
        return normalized
    if isinstance(value, (list, tuple)):
        return [
            normalize_json(item, f"{field}[{index}]")
            for index, item in enumerate(value)
        ]
    raise ContractValidationError(
        field,
        f"contains non-JSON value of type {type(value).__name__}",
    )


def normalize_json_object(
    value: Mapping[str, object] | object,
    field: str,
) -> dict[str, JSONValue]:
    mapping = require_mapping(value, field)
    normalized = normalize_json(mapping, field)
    if not isinstance(normalized, dict):
        raise ContractValidationError(field, "must be an object")
    return normalized


def normalize_string_tuple(
    values: Sequence[str] | object,
    field: str,
    *,
    unique: bool = True,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ContractValidationError(field, "must be an array of strings")
    normalized = tuple(
        require_string(value, f"{field}[{index}]")
        for index, value in enumerate(values)
    )
    if unique and len(set(normalized)) != len(normalized):
        raise ContractValidationError(field, "must not contain duplicates")
    return normalized

