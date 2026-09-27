"""Retain bounded execution decisions without command inputs or resource identities."""

from __future__ import annotations

import logging
import re
from typing import Any, Literal

from .management_access import AccessError, InspectionAccess
from .management_records import ACTION_CAPABILITIES, OperationError, utc_now, uuid_string
from .management_registry import ManagementRegistry

AuditSource = Literal["core", "mcp", "cli", "worker"]
AuditEvent = Literal[
    "permission_decision",
    "operation_accepted",
    "cancellation_requested",
    "stage_started",
    "stage_finished",
    "operation_finished",
    "recovery_required",
    "cli_started",
    "cli_finished",
    "tool_finished",
]
_LOGGER = logging.getLogger(__name__)
_TOOLS = {
    "plan_prepare",
    "plan_execute",
    "operation_resume",
    "operation_cancel",
    "operation_inspect",
    "operation_list",
    "plan_inspect",
    "narwhal",
    "narwhal-engine",
    "narwhal-check",
    "narwhal-profile",
}


def _identifier(value: Any) -> str | None:
    try:
        return uuid_string(value)
    except OperationError:
        return None


def execution_event(
    registry: ManagementRegistry,
    event: AuditEvent,
    *,
    source: AuditSource,
    target_id: str | None,
    tool: str | None = None,
    action: str | None = None,
    operation: dict[str, Any] | None = None,
    outcome: str,
    codes: list[str] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    decision: Literal["allow", "deny"] | None = None,
    inspect_only: bool = False,
    required: bool = True,
) -> None:
    """Persist a receipt before effects, or report a failed post-commit receipt.

    Callers use required=False only after committing an outcome or during cleanup.
    Such failures leave durable operation evidence intact and log fixed prose.
    """
    access = InspectionAccess(registry)
    target = next((target for target in registry.targets if target.id == target_id), None)
    action = action if action in ACTION_CAPABILITIES else None
    record = operation or {}
    tool = tool or record.get("tool")
    references = artifacts if artifacts is not None else record.get("artifacts", [])
    evidence = sorted(
        {
            identifier
            for reference in references
            if isinstance(reference, dict)
            and (identifier := _identifier(reference.get("artifact_id"))) is not None
        }
    )
    stage_id = record.get("current_stage")
    row: dict[str, Any] = {
        "observed_at": utc_now(),
        "event": event,
        "source": source,
        "tool": tool if tool in _TOOLS else None,
        "target_id": target.id if target is not None else None,
        "action": action,
        "operation_id": _identifier(record.get("operation_id")),
        "plan_id": _identifier(record.get("plan_id"))
        or _identifier(((record.get("result") or {}).get("data") or {}).get("plan_id")),
        "request_id": _identifier(record.get("request_id")),
        "stage_id": stage_id
        if isinstance(stage_id, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", stage_id)
        else None,
        "outcome": outcome,
        "error_codes": [
            code if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) else "adapter_failed"
            for code in (codes or [])[:64]
        ],
        "artifact_ids": evidence[:256],
        "artifact_count": len(evidence),
        "artifact_ids_truncated": len(evidence) > 256,
    }
    if decision is not None:
        required_capabilities = {"inspect"}
        if action is not None and not inspect_only:
            required_capabilities |= ACTION_CAPABILITIES[action]
        row["permission"] = {
            "decision": decision,
            "required_capabilities": sorted(required_capabilities),
            "granted_capabilities": sorted(target.capabilities) if target else [],
            "action_allowed": bool(target and action in target.actions) if action else None,
        }
    # Apply the same credential and request-content redaction used for exports.
    # Unknown target selectors never enter a receipt, and no raw exception text is accepted.
    for registered in registry.targets if target is None else [target]:
        row = access.redactor(registered).value(row)
    try:
        access.write_audit("execution", row)
    except (AccessError, OSError):
        if required:
            raise OperationError(
                "audit_failed", "Execution audit storage is unavailable or unsafe"
            ) from None
        _LOGGER.error("Execution audit receipt could not be retained; inspect the operation record")


def operation_event(
    registry: ManagementRegistry,
    event: AuditEvent,
    operation: dict[str, Any],
    *,
    source: AuditSource = "worker",
    outcome: str,
    required: bool = True,
    stage_id: str | None = None,
    codes: list[str] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
) -> None:
    """Project correlation fields from a committed operation into its audit receipt."""
    execution_event(
        registry,
        event,
        source=source,
        target_id=operation.get("target_id"),
        action=operation.get("action"),
        operation={**operation, "current_stage": stage_id or operation.get("current_stage")},
        outcome=outcome,
        codes=codes,
        artifacts=artifacts,
        required=required,
    )
