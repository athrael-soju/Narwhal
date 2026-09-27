"""Redact operation evidence before storing public immutable exports."""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from .management_access import InspectionAccess
from .management_registry import ManagementTarget


def public_value(access: InspectionAccess, target: ManagementTarget, value: Any) -> Any:
    """Remove credential values and private destinations while retaining opaque IDs."""
    redactor = access.redactor(target)

    def prepare(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                key: None if key == "launch_token" else prepare(child)
                for key, child in item.items()
            }
        if isinstance(item, list):
            return [prepare(child) for child in item]
        return item

    prepared = prepare(value)
    redactor.references(prepared)
    public = redactor.value(prepared)

    def scrub(item: Any, original: Any) -> Any:
        if isinstance(item, str):
            item = re.sub(r"(?:https?|ssh)://[^\s\"'<>]+", "[PRIVATE_ENDPOINT]", item)
            item = re.sub(r"(?<![\w:])/(?:[^\s\"'<>]+)", "[PRIVATE_PATH]", item)
            item = re.sub(r"[\w.-]+@[\w.:-]+", "[PRIVATE_DESTINATION]", item)
            item = re.sub(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?", "[PRIVATE_ADDRESS]", item)
            return item
        if isinstance(item, dict):
            result = {}
            for key, child in item.items():
                if key == "launch_token":
                    try:
                        result[key] = str(UUID(original[key])) if original[key] else None
                    except (TypeError, ValueError, AttributeError):
                        result[key] = "[REDACTED]"
                elif child is not None and re.search(
                    r"(?:^|_)(?:address|hostname|destination|ssh_host)$", key
                ):
                    result[key] = "[PRIVATE_DESTINATION]"
                else:
                    result[key] = scrub(child, original.get(key))
            return result
        if isinstance(item, list):
            return [scrub(child, original[index]) for index, child in enumerate(item)]
        return item

    return scrub(public, value)


def reject_credentials(
    access: InspectionAccess, target: ManagementTarget, retained: list[bytes]
) -> None:
    """Refuse known secret values and literal credential fields before private persistence."""
    import json

    from .management_records import OperationError

    redactor = access.redactor(target)

    def literal(item: Any) -> bool:
        if isinstance(item, dict):
            for key, child in item.items():
                if key.endswith("_env") or key == "launch_token":
                    continue
                if (
                    re.search(
                        r"(?:^|[_-])(?:password|passwd|secret|token|api[_-]?key|authorization|private[_-]?key|credentials?|access[_-]?key)$",
                        key,
                        re.I,
                    )
                    and child
                ):
                    return True
                if literal(child):
                    return True
        if isinstance(item, list):
            return any(literal(child) for child in item)
        return False

    for data in retained:
        if any(secret.encode() in data for secret in redactor.secrets):
            raise OperationError("permission_denied", "Prepared inputs contain credential values")
        try:
            value = json.loads(data)
        except (ValueError, UnicodeError):
            if re.search(rb"(?i)(?:password|secret|api[_-]?key|authorization)\s*[:=]\s*\S", data):
                raise OperationError(
                    "permission_denied", "Prepared inputs contain literal credentials"
                ) from None
        else:
            if literal(value):
                raise OperationError(
                    "permission_denied", "Prepared inputs contain literal credentials"
                )
