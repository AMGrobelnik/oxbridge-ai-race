"""Canonical JSON and SHA-256 helpers shared by every hash in Sealed (SPEC.md section 7)."""

import hashlib
import json
from typing import Any


def round_floats(value: Any, ndigits: int = 6) -> Any:
    """Returns a copy with every float rounded, so hashes do not depend on float repr noise."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float):
        return round(value, ndigits)
    if isinstance(value, dict):
        return {str(k): round_floats(v, ndigits) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [round_floats(v, ndigits) for v in value]
    return value


def canonical_json(value: Any) -> bytes:
    """Sorted keys, compact separators, UTF-8, floats rounded to 6 decimals, NaN rejected."""
    return json.dumps(round_floats(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_hex(canonical_json(value))
