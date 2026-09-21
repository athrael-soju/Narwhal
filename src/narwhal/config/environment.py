"""Resolve explicit environment references in fleet endpoint fields."""

from __future__ import annotations

import os
import re

_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def resolve_endpoint(value: str, field: str) -> str:
    """Resolve a whole-value ${VARIABLE} endpoint without expanding other fields."""
    if "${" not in value:
        return value
    match = _REFERENCE.fullmatch(value)
    if match is None:
        raise ValueError(f"{field} requires a whole-value ${{VARIABLE}} reference")
    name = match[1]
    resolved = os.environ.get(name, "")
    if not resolved.strip():
        raise ValueError(f"{field}: environment variable {name} is unset or empty")
    if "${" in resolved:
        raise ValueError(f"{field}: environment variable {name} contains another reference")
    return resolved
