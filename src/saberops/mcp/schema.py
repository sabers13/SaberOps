"""A deliberately small, fail-closed JSON-Schema subset validator.

The MCP interface must refuse malformed arguments *before* any service call,
and must refuse unknown provider/model/billing fields outright (ADR-0001
invariant 3).  Instead of taking a dependency, this module implements exactly
the keywords the SaberOps tool schemas use:

``type`` (incl. type unions), ``required``, ``properties``,
``additionalProperties`` (bool), ``enum``, ``const``, ``items``,
``minItems``, ``maxItems``, ``uniqueItems``, ``minimum``, ``maximum``,
``minLength``, ``maxLength`` and ``pattern``.

Anything the validator does not understand is reported as an unknown keyword
so a schema can never silently degrade into "accepts anything".
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

__all__ = ["SchemaError", "validate_instance"]

_KNOWN_KEYWORDS: frozenset[str] = frozenset(
    {
        "type",
        "required",
        "properties",
        "additionalProperties",
        "enum",
        "const",
        "items",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "pattern",
        "title",
        "description",
        "default",
    }
)

_TYPE_NAMES: frozenset[str] = frozenset(
    {"object", "array", "string", "integer", "number", "boolean", "null"}
)


class SchemaError(ValueError):
    """A schema definition itself is malformed (a programming error)."""


def _type_of(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, Sequence):
        return "array"
    return type(value).__name__


def _matches_type(value: Any, expected: str) -> bool:
    actual = _type_of(value)
    if expected == "number":
        return actual in ("number", "integer")
    return actual == expected


def _check_schema(schema: object, *, path: str) -> None:
    if not isinstance(schema, Mapping):
        raise SchemaError(f"{path}: schema must be an object")
    unknown = set(schema) - _KNOWN_KEYWORDS
    if unknown:
        raise SchemaError(f"{path}: unknown schema keyword(s): {sorted(unknown)}")
    declared = schema.get("type")
    if declared is not None:
        names = declared if isinstance(declared, list) else [declared]
        for name in names:
            if name not in _TYPE_NAMES:
                raise SchemaError(f"{path}: unknown type {name!r}")
    props = schema.get("properties")
    if props is not None:
        if not isinstance(props, Mapping):
            raise SchemaError(f"{path}: properties must be an object")
        for key, sub in props.items():
            _check_schema(sub, path=f"{path}.{key}")
    items = schema.get("items")
    if items is not None:
        _check_schema(items, path=f"{path}[]")
    additional = schema.get("additionalProperties")
    if additional is not None and not isinstance(additional, bool):
        _check_schema(additional, path=f"{path}.*")


def validate_instance(schema: Mapping[str, Any], instance: Any) -> list[str]:
    """Return a list of human-readable violations; empty means valid."""
    _check_schema(schema, path="$")
    errors: list[str] = []
    _validate(schema, instance, path="$", errors=errors)
    return errors


def _validate(
    schema: Mapping[str, Any], instance: Any, *, path: str, errors: list[str]
) -> None:
    declared = schema.get("type")
    if declared is not None:
        names = declared if isinstance(declared, list) else [declared]
        if not any(_matches_type(instance, str(name)) for name in names):
            errors.append(
                f"{path}: expected {'|'.join(str(n) for n in names)}, got {_type_of(instance)}"
            )
            return

    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: must equal {schema['const']!r}")

    enum = schema.get("enum")
    if enum is not None and instance not in enum:
        errors.append(f"{path}: must be one of {list(enum)}")

    if isinstance(instance, str):
        _validate_string(schema, instance, path=path, errors=errors)
    if isinstance(instance, bool):
        return
    if isinstance(instance, (int, float)):
        _validate_number(schema, instance, path=path, errors=errors)
    if isinstance(instance, Mapping):
        _validate_object(schema, instance, path=path, errors=errors)
    if isinstance(instance, Sequence) and not isinstance(instance, (str, bytes, bytearray)):
        _validate_array(schema, instance, path=path, errors=errors)


def _validate_string(
    schema: Mapping[str, Any], value: str, *, path: str, errors: list[str]
) -> None:
    min_length = schema.get("minLength")
    if isinstance(min_length, int) and len(value) < min_length:
        errors.append(f"{path}: must be at least {min_length} character(s)")
    max_length = schema.get("maxLength")
    if isinstance(max_length, int) and len(value) > max_length:
        errors.append(f"{path}: must be at most {max_length} character(s)")
    pattern = schema.get("pattern")
    if isinstance(pattern, str) and re.search(pattern, value) is None:
        errors.append(f"{path}: does not match pattern {pattern!r}")


def _validate_number(
    schema: Mapping[str, Any], value: float, *, path: str, errors: list[str]
) -> None:
    minimum = schema.get("minimum")
    if isinstance(minimum, (int, float)) and value < minimum:
        errors.append(f"{path}: must be >= {minimum}")
    maximum = schema.get("maximum")
    if isinstance(maximum, (int, float)) and value > maximum:
        errors.append(f"{path}: must be <= {maximum}")


def _validate_object(
    schema: Mapping[str, Any], value: Mapping[str, Any], *, path: str, errors: list[str]
) -> None:
    properties = schema.get("properties")
    props: Mapping[str, Any] = properties if isinstance(properties, Mapping) else {}
    for name in schema.get("required", []):
        if name not in value:
            errors.append(f"{path}: missing required property {name!r}")
    additional = schema.get("additionalProperties", True)
    for key, item in value.items():
        child = props.get(key)
        if child is not None:
            _validate(child, item, path=f"{path}.{key}", errors=errors)
        elif additional is False:
            errors.append(f"{path}: unexpected property {key!r}")
        elif isinstance(additional, Mapping):
            _validate(additional, item, path=f"{path}.{key}", errors=errors)


def _validate_array(
    schema: Mapping[str, Any], value: Sequence[Any], *, path: str, errors: list[str]
) -> None:
    items = schema.get("items")
    min_items = schema.get("minItems")
    if isinstance(min_items, int) and len(value) < min_items:
        errors.append(f"{path}: needs at least {min_items} item(s)")
    max_items = schema.get("maxItems")
    if isinstance(max_items, int) and len(value) > max_items:
        errors.append(f"{path}: must have at most {max_items} item(s)")
    if schema.get("uniqueItems") is True:
        seen: list[str] = []
        for item in value:
            rendered = repr(item)
            if rendered in seen:
                errors.append(f"{path}: items must be unique")
                break
            seen.append(rendered)
    if isinstance(items, Mapping):
        for index, item in enumerate(value):
            _validate(items, item, path=f"{path}[{index}]", errors=errors)
