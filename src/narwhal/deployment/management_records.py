"""Validate durable operation records and their bounded public summaries."""

from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from narwhal.command_results import EXIT_CODES
from narwhal.contracts import COMMAND_RESULT, ContractVersionError, validate_document

MAX_RECORD_BYTES = 8 * 1024 * 1024
MAX_ACTIVE_RECORD_BYTES = 7 * 1024 * 1024
MAX_STAGES = 256
MAX_RESOURCES = 4096
TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
TRANSITIONS = {
    "queued": {"running", "failed", "cancelled"},
    "running": {"succeeded", "failed", "cancelling", "recovery_required"},
    "cancelling": {"cancelled", "recovery_required"},
    "recovery_required": {"succeeded", "failed", "cancelled"},
    "succeeded": set(),
    "failed": set(),
    "cancelled": set(),
}
ACTION_CAPABILITIES = {
    "dev_init": {"mutate"},
    "dev_up": {"measure", "mutate"},
    "dev_verify": {"measure"},
    "dev_down": {"mutate"},
    "fleet_deploy": {"measure", "mutate"},
    "fleet_profile": {"measure"},
    "fleet_preflight": {"measure"},
    "engine_replace": {"measure", "mutate"},
    "monitoring_start": {"mutate"},
    "deployment_cleanup": {"mutate"},
}
_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_REQUIRED = {
    "schema",
    "schema_version",
    "operation_id",
    "target_id",
    "request_id",
    "parent_operation_id",
    "tool",
    "action",
    "plan_id",
    "plan_digest",
    "state",
    "revision",
    "created_at",
    "updated_at",
    "started_at",
    "finished_at",
    "deadline_at",
    "preparation_budgets",
    "current_stage",
    "stages",
    "resources",
    "worker",
    "cancellation",
    "recovery",
    "result",
    "artifacts",
}


class OperationError(ValueError):
    """Report a durable-store error without disclosing private record contents."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def utc_now() -> str:
    """Return an RFC 3339 timestamp in UTC."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def require(condition: bool, message: str = "Operation record is invalid") -> None:
    if not condition:
        raise OperationError("invalid_input", message)


def uuid_string(value: Any) -> str:
    """Validate a UUID string without accepting a path or exposing its contents."""
    require(isinstance(value, str) and len(value) == 36, "Identifier must be a UUID")
    try:
        parsed = UUID(value)
    except ValueError:
        raise OperationError("invalid_input", "Identifier must be a UUID") from None
    require(str(parsed) == value.lower(), "Identifier must be a UUID")
    return str(parsed)


def _json_value(value: Any, *, floats: bool, depth: int = 0) -> None:
    require(depth <= 64, "Operation data exceeds its nesting limit")
    if value is None or type(value) in {str, bool, int}:
        return
    if type(value) is float:
        require(floats and math.isfinite(value), "Operation data contains an unsupported number")
    elif isinstance(value, list):
        for item in value:
            _json_value(item, floats=floats, depth=depth + 1)
    elif isinstance(value, dict):
        require(all(isinstance(key, str) for key in value), "JSON object keys must be strings")
        for item in value.values():
            _json_value(item, floats=floats, depth=depth + 1)
    else:
        raise OperationError("invalid_input", "Operation data is not JSON")


def canonical(value: Any) -> bytes:
    """Encode canonical request or plan JSON, rejecting all floating-point values."""
    _json_value(value, floats=False)
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    except (UnicodeError, ValueError):
        raise OperationError("invalid_input", "Operation data cannot be encoded") from None


def encode_record(value: dict[str, Any]) -> bytes:
    """Encode finite record JSON, preserving numeric command evidence and added fields."""
    _json_value(value, floats=True)
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    except (UnicodeError, ValueError):
        raise OperationError("invalid_input", "Operation record cannot be encoded") from None


def _keys(value: Any, names: set[str]) -> None:
    require(isinstance(value, dict) and names <= value.keys())


def _alias(value: Any) -> None:
    require(isinstance(value, str) and _ALIAS.fullmatch(value) is not None)


def _timestamp(value: Any, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    require(isinstance(value, str) and value.endswith("Z"))
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise OperationError("invalid_input", "Operation timestamp is invalid") from None
    require(parsed.tzinfo == UTC)


def _positive(value: Any) -> None:
    require(type(value) is int and value > 0)


def _errors(value: Any) -> None:
    require(isinstance(value, list))
    for error in value:
        _keys(error, {"code", "message"})
        require(isinstance(error["code"], str) and isinstance(error["message"], str))
        require(isinstance(error.get("context", {}), dict))


def _artifacts(value: Any) -> None:
    require(isinstance(value, list))
    for artifact in value:
        _keys(
            artifact,
            {"artifact_id", "kind", "state", "sha256", "size_bytes", "observed_at", "complete"},
        )
        uuid_string(artifact["artifact_id"])
        require(isinstance(artifact["kind"], str))
        require(artifact["state"] in {"created", "updated", "existing", "missing"})
        require(
            artifact["sha256"] is None
            or (
                isinstance(artifact["sha256"], str)
                and _DIGEST.fullmatch(artifact["sha256"]) is not None
            )
        )
        require(
            artifact["size_bytes"] is None
            or (type(artifact["size_bytes"]) is int and artifact["size_bytes"] >= 0)
        )
        require(type(artifact["complete"]) is bool)
        _timestamp(artifact["observed_at"])


def _command(value: Any) -> None:
    if value is None:
        return
    validate_document(value, COMMAND_RESULT)
    require(value.get("status") in EXIT_CODES)
    require(
        type(value.get("exit_code")) is int and value["exit_code"] == EXIT_CODES[value["status"]]
    )
    require(isinstance(value.get("data"), dict) and isinstance(value.get("artifacts"), list))
    _errors(value.get("errors"))


def budgets(value: Any) -> int:
    """Validate preparation budgets and return their total milliseconds."""
    names = {"timeout_ms", "term_grace_ms", "kill_grace_ms", "reconcile_ms"}
    _keys(value, names)
    for name in names:
        require(type(value[name]) is int and 1 <= value[name] <= 86_400_000)
    return sum(value[name] for name in names)


def _validate_record(record: dict[str, Any]) -> None:
    """Check required structure and state invariants while retaining compatible fields."""
    _keys(record, _REQUIRED)
    if (
        record["schema"] != "narwhal.management-operation"
        or type(record["schema_version"]) is not int
        or record["schema_version"] != 1
    ):
        raise ContractVersionError("Operation document contract is unsupported")
    for name in ("operation_id", "request_id"):
        uuid_string(record[name])
    if record["parent_operation_id"] is not None:
        uuid_string(record["parent_operation_id"])
    _alias(record["target_id"])
    require(record["tool"] in {"plan_prepare", "plan_execute", "operation_resume"})
    require(record["action"] in ACTION_CAPABILITIES)
    require(record["state"] in TRANSITIONS)
    _positive(record["revision"])
    for name in ("created_at", "updated_at"):
        _timestamp(record[name])
    for name in ("started_at", "finished_at", "deadline_at"):
        _timestamp(record[name], nullable=True)
    if record["tool"] == "plan_prepare":
        require(record["plan_id"] is None and record["plan_digest"] is None)
        budgets(record["preparation_budgets"])
        require(record["parent_operation_id"] is None)
    else:
        uuid_string(record["plan_id"])
        require(
            isinstance(record["plan_digest"], str)
            and _DIGEST.fullmatch(record["plan_digest"]) is not None
        )
        require(record["preparation_budgets"] is None)
        require(
            (record["parent_operation_id"] is not None) == (record["tool"] == "operation_resume")
        )
    stages = record["stages"]
    require(isinstance(stages, list) and len(stages) <= MAX_STAGES)
    stage_ids = set()
    effect_count = 0
    for stage in stages:
        _keys(
            stage,
            {
                "stage_id",
                "state",
                "started_at",
                "finished_at",
                "command_result",
                "artifacts",
                "effects",
                "reused_from",
            },
        )
        _alias(stage["stage_id"])
        require(stage["stage_id"] not in stage_ids)
        stage_ids.add(stage["stage_id"])
        require(
            stage["state"] in {"pending", "running", "succeeded", "failed", "cancelled", "reused"}
        )
        _timestamp(stage["started_at"], nullable=True)
        _timestamp(stage["finished_at"], nullable=True)
        if stage["state"] == "pending":
            require(stage["started_at"] is None and stage["finished_at"] is None)
        elif stage["state"] == "running":
            require(stage["started_at"] is not None and stage["finished_at"] is None)
        elif stage["state"] != "reused":
            require(stage["finished_at"] is not None)
        _command(stage["command_result"])
        _artifacts(stage["artifacts"])
        require(isinstance(stage["effects"], list))
        effect_count += len(stage["effects"])
        for effect in stage["effects"]:
            _keys(
                effect,
                {"resource_id", "kind", "host_id", "owner", "identity", "effect", "observed_at"},
            )
            require(isinstance(effect["resource_id"], str) and bool(effect["resource_id"]))
            require(
                effect["kind"]
                in {
                    "engine",
                    "attestation",
                    "router",
                    "monitoring",
                    "tunnel",
                    "measurement_helper",
                    "installation",
                    "private_artifact",
                }
            )
            _alias(effect["host_id"])
            _keys(effect["owner"], {"operation_id", "stage_id", "launch_token"})
            uuid_string(effect["owner"]["operation_id"])
            _alias(effect["owner"]["stage_id"])
            require(effect["identity"] is None or isinstance(effect["identity"], dict))
            require(effect["effect"] in {"confirmed", "absent", "unknown"})
            if effect["effect"] == "confirmed":
                require(effect["identity"] is not None)
            _timestamp(effect["observed_at"])
        if stage["reused_from"] is not None:
            _keys(stage["reused_from"], {"operation_id", "stage_id"})
            uuid_string(stage["reused_from"]["operation_id"])
            _alias(stage["reused_from"]["stage_id"])
    require(effect_count <= MAX_RESOURCES, "Operation effect count exceeds its limit")
    if record["current_stage"] is not None:
        require(
            record["current_stage"] in stage_ids
            or (record["tool"] == "plan_prepare" and record["current_stage"] == "discovery")
        )
    require(isinstance(record["resources"], list) and len(record["resources"]) <= MAX_RESOURCES)
    for resource in record["resources"]:
        _keys(resource, {"resource_id", "mode", "operation_id", "fence", "acquired_at"})
        require(isinstance(resource["resource_id"], str) and bool(resource["resource_id"]))
        require(
            resource["mode"] == "exclusive" and resource["operation_id"] == record["operation_id"]
        )
        _positive(resource["fence"])
        _timestamp(resource["acquired_at"])
    if record["worker"] is not None:
        worker = record["worker"]
        _keys(
            worker,
            {"worker_id", "host_id", "boot_id", "pid", "start_ticks", "heartbeat_at", "fence"},
        )
        uuid_string(worker["worker_id"])
        uuid_string(worker["boot_id"])
        _alias(worker["host_id"])
        for name in ("pid", "start_ticks", "fence"):
            _positive(worker[name])
        _timestamp(worker["heartbeat_at"])
    _keys(record["cancellation"], {"requested_at", "reason"})
    _timestamp(record["cancellation"]["requested_at"], nullable=True)
    require(
        record["cancellation"]["reason"] is None
        or isinstance(record["cancellation"]["reason"], str)
    )
    _keys(record["recovery"], {"reason", "observed_at", "errors", "required_actions", "artifacts"})
    require(record["recovery"]["reason"] is None or isinstance(record["recovery"]["reason"], str))
    _timestamp(record["recovery"]["observed_at"], nullable=True)
    _errors(record["recovery"]["errors"])
    require(isinstance(record["recovery"]["required_actions"], list))
    _artifacts(record["recovery"]["artifacts"])
    _artifacts(record["artifacts"])
    if record["state"] in TERMINAL:
        require(record["finished_at"] is not None and record["current_stage"] is None)
        result = record["result"]
        _keys(result, {"status", "data", "errors", "artifacts", "command_result"})
        allowed = {
            "succeeded": {"success", "degraded"},
            "failed": {"failed_gate", "invalid_input", "error"},
            "cancelled": {"interrupted"},
        }
        require(result["status"] in allowed[record["state"]])
        require(isinstance(result["data"], dict))
        key = (
            "plan_id"
            if record["tool"] == "plan_prepare" and record["state"] == "succeeded"
            else "summary_artifact_id"
        )
        uuid_string(result["data"].get(key))
        _errors(result["errors"])
        _artifacts(result["artifacts"])
        _command(result["command_result"])
        require(
            not any(
                effect["effect"] == "unknown" for stage in stages for effect in stage["effects"]
            ),
            "Unknown effects require recovery",
        )
    else:
        require(record["result"] is None and record["finished_at"] is None)
    if record["state"] == "queued":
        require(
            record["started_at"] is None
            and record["deadline_at"] is None
            and record["worker"] is None
        )
    if record["state"] in {"running", "cancelling"}:
        require(
            record["worker"] is not None
            and record["started_at"] is not None
            and record["deadline_at"] is not None
        )
    if record["state"] == "succeeded":
        require(all(stage["state"] in {"succeeded", "reused"} for stage in stages))
    maximum = MAX_RECORD_BYTES if record["state"] in TERMINAL else MAX_ACTIVE_RECORD_BYTES
    require(len(encode_record(record)) <= maximum, "Operation record exceeds its byte limit")


def validate_record(record: dict[str, Any]) -> None:
    """Reject malformed records without exposing parser errors or persisted values."""
    try:
        _validate_record(record)
    except (KeyError, TypeError, AttributeError, OverflowError, RecursionError):
        raise OperationError("invalid_input", "Operation record is invalid") from None


def new_operation(
    *,
    target_id: str,
    request_id: str,
    tool: str,
    action: str,
    plan: dict[str, Any] | None = None,
    parent_operation_id: str | None = None,
    preparation_budgets: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an unassigned queued record from already validated admission inputs."""
    now = utc_now()
    stages = [{"stage_id": "discovery"}] if plan is None else plan["payload"]["stages"]
    record: dict[str, Any] = {
        "schema": "narwhal.management-operation",
        "schema_version": 1,
        "operation_id": str(uuid4()),
        "target_id": target_id,
        "request_id": request_id,
        "parent_operation_id": parent_operation_id,
        "tool": tool,
        "action": action,
        "plan_id": plan["plan_id"] if plan else None,
        "plan_digest": plan["plan_digest"] if plan else None,
        "state": "queued",
        "revision": 1,
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "finished_at": None,
        "deadline_at": None,
        "preparation_budgets": deepcopy(preparation_budgets),
        "current_stage": None,
        "stages": [
            {
                "stage_id": stage["stage_id"],
                "state": "pending",
                "started_at": None,
                "finished_at": None,
                "command_result": None,
                "artifacts": [],
                "effects": [],
                "reused_from": None,
            }
            for stage in stages
        ],
        "resources": [],
        "worker": None,
        "cancellation": {"requested_at": None, "reason": None},
        "recovery": {
            "reason": None,
            "observed_at": None,
            "errors": [],
            "required_actions": [],
            "artifacts": [],
        },
        "result": None,
        "artifacts": [],
    }
    validate_record(record)
    return record


def summary(record: dict[str, Any]) -> dict[str, Any]:
    """Return the documented operation projection without unbounded evidence bodies."""
    result = record["result"]
    errors = result["errors"] if result is not None else record["recovery"]["errors"]
    result_data = None
    if result:
        key = (
            "plan_id"
            if record["tool"] == "plan_prepare" and record["state"] == "succeeded"
            else "summary_artifact_id"
        )
        result_data = {key: result["data"][key]}
    fields = (
        "operation_id",
        "parent_operation_id",
        "action",
        "plan_id",
        "state",
        "current_stage",
        "revision",
        "created_at",
        "updated_at",
        "finished_at",
    )
    return {
        **{name: deepcopy(record[name]) for name in fields},
        "result_status": result["status"] if result else None,
        "result_data": result_data,
        "error_codes": [error["code"] for error in errors[:10]],
        "error_count": len(errors),
        "stage_count": len(record["stages"]),
        "resource_count": len(record["resources"]),
    }
