"""A small JSON Schema subset: the parts the race verifier checks (type, required, properties,
additionalProperties, items, enum, minimum/maximum, minLength/maxLength, minItems/maxItems,
format "date").

validate() lists problems in plain words, so they can be shown to the model for a retry.
conform() fixes what is safe to fix without guessing: numbers sent as strings, whole floats where an
integer is required, and properties the schema forbids.
"""

import re
from typing import Any

Schema = dict[str, Any]

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NUMBER = re.compile(r"^-?\d+(?:\.\d+)?$")


def _type_of(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int) or (isinstance(value, float) and value.is_integer()):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def validate(value: Any, schema: Schema, path: str = "") -> list[str]:
    """Problems with value against schema; empty when it fits."""
    at = path or "the result"
    expected = schema.get("type")
    if expected:
        actual = _type_of(value)
        fits = actual in ("number", "integer") if expected == "number" else actual == expected
        if not fits:
            return [f"{at}: expected {expected}, got {actual}"]
    problems: list[str] = []
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{at}: must be one of {schema['enum']}")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            problems.append(f"{at}: too short")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            problems.append(f"{at}: too long")
        if schema.get("format") == "date" and not _DATE.match(value):
            problems.append(f"{at}: must be a date like 2026-10-25")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            problems.append(f"{at}: below {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            problems.append(f"{at}: above {schema['maximum']}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            problems.append(f"{at}: needs at least {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            problems.append(f"{at}: at most {schema['maxItems']} items")
        if isinstance(schema.get("items"), dict):
            for i, item in enumerate(value):
                problems += validate(item, schema["items"], f"{path}[{i}]")
    if isinstance(value, dict):
        properties: dict[str, Schema] = schema.get("properties") or {}
        prefix = f"{path}." if path else ""
        problems += [f"{prefix}{key}: missing" for key in schema.get("required", []) if key not in value]
        for key, sub in properties.items():
            if key in value:
                problems += validate(value[key], sub, f"{prefix}{key}")
        if schema.get("additionalProperties") is False:
            problems += [f"{prefix}{key}: not allowed" for key in value if key not in properties]
    return problems


def conform(value: Any, schema: Schema) -> Any:
    """Returns value with safe, lossless fixes applied so it fits schema better."""
    expected = schema.get("type")
    if expected in ("integer", "number") and isinstance(value, str) and _NUMBER.match(value.strip()):
        value = float(value.strip())
    if expected == "integer" and isinstance(value, float) and value.is_integer():
        return int(value)
    if expected == "string" and isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        return [conform(item, schema["items"]) for item in value]
    if isinstance(value, dict):
        properties: dict[str, Schema] = schema.get("properties") or {}
        out = {k: conform(v, properties[k]) if k in properties else v for k, v in value.items()}
        if schema.get("additionalProperties") is False:
            out = {k: v for k, v in out.items() if k in properties}
        return out
    return value
