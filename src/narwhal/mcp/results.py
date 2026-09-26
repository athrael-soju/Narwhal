"""Validate management envelopes and preserve finite command outcomes."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from narwhal.command_results import EXIT_CODES
from narwhal.contracts import COMMAND_RESULT, MANAGEMENT_RESULT, validate_document, versioned

Alias = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")]
Outcome = Literal[
    "success", "accepted", "degraded", "failed_gate", "invalid_input", "error", "interrupted"
]
Timestamp = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")]
UUIDString = Annotated[str, Field(pattern=r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")]


class WireModel(BaseModel):
    """Strict JSON objects shared by the management protocol."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class ManagementError(WireModel):
    """A stable code with adapter-redacted explanatory context."""

    code: str
    message: str
    context: dict[str, Any]


class ArtifactReference(WireModel):
    """An adapter-exported immutable artifact, never an arbitrary read path."""

    artifact_id: UUIDString
    kind: str
    state: Literal["created", "updated", "existing", "missing"]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None
    size_bytes: Annotated[int, Field(ge=0)] | None
    observed_at: Timestamp
    complete: bool


class ManagementResult(WireModel):
    """Version one envelope advertised as every tool's output schema."""

    schema_id: Literal["narwhal.management-result"] = Field(alias="schema")
    schema_version: Literal[1]
    tool: Alias
    target_id: Alias | None
    observed_at: Timestamp
    outcome: Outcome
    data: dict[str, Any]
    errors: list[ManagementError]
    artifacts: list[ArtifactReference]
    command_result: dict[str, Any] | None


def result(
    tool: str,
    target_id: str | None = None,
    outcome: Outcome = "success",
    data: dict[str, Any] | None = None,
    errors: list[dict[str, Any]] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    command_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an envelope from data already redacted by its owning adapter."""
    return versioned(
        MANAGEMENT_RESULT,
        {
            "tool": tool,
            "target_id": target_id,
            "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "outcome": outcome,
            "data": deepcopy(data or {}),
            "errors": deepcopy(errors or []),
            "artifacts": deepcopy(artifacts or []),
            "command_result": deepcopy(command_result),
        },
    )


def from_command_result(
    tool: str,
    target_id: str | None,
    document: dict[str, Any],
    *,
    artifacts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Map a redacted command document without rewriting its codes or evidence."""
    validate_document(document, COMMAND_RESULT)
    status = document.get("status")
    if not isinstance(status, str) or status not in EXIT_CODES:
        raise ValueError("command result has an unsupported status")
    exit_code = document.get("exit_code")
    if type(exit_code) is not int or exit_code != EXIT_CODES[status]:
        raise ValueError("command result has an inconsistent exit code")
    if not isinstance(document.get("data"), dict) or not isinstance(
        document.get("artifacts"), list
    ):
        raise ValueError("command result has malformed data or artifacts")
    if not isinstance(document.get("errors"), list):
        raise ValueError("command result has malformed errors")
    errors = []
    for error in document["errors"]:
        if not isinstance(error, dict) or not all(
            isinstance(error.get(key), str) for key in ("code", "message")
        ):
            raise ValueError("command result has a malformed error")
        context = error.get("context", {})
        if not isinstance(context, dict):
            raise ValueError("command result has malformed error context")
        errors.append(
            {
                "code": error["code"],
                "message": error["message"],
                "context": {
                    **{
                        key: value
                        for key, value in error.items()
                        if key not in {"code", "message", "context"}
                    },
                    **context,
                },
            }
        )
    # Pydantic verifies the narrowed outcome and leaves the original document intact.
    payload = result(
        tool,
        target_id,
        data=document["data"],
        errors=errors,
        artifacts=artifacts,
        command_result=document,
    )
    payload["outcome"] = status
    return ManagementResult.model_validate(payload).model_dump(by_alias=True)
