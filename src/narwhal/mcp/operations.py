"""Expose retained plans and operations through the shared coordinator."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
from typing import Any

from pydantic import Field

from narwhal.contracts import ContractVersionError
from narwhal.deployment.management_access import AccessError, InspectionAccess
from narwhal.deployment.management_audit import execution_event
from narwhal.deployment.management_coordinator import OperationCoordinator, View
from narwhal.deployment.management_records import OperationError
from narwhal.deployment.management_registry import ManagementRegistry, PlanAction
from narwhal.diagnostics.management_artifacts import ArtifactError, ArtifactStore

from .adapters import MAX_RESULT_BYTES, ToolAdapter
from .inspection import TargetInput
from .results import Outcome, UUIDString, result


class OperationListInput(TargetInput):
    """Select an immutable page of operation summaries for one target."""

    limit: int = Field(default=20, ge=1, le=100)
    cursor: UUIDString | None = None


class OperationInput(TargetInput):
    """Select one operation belonging to the registered target."""

    operation_id: UUIDString


class PlanInput(TargetInput):
    """Select one retained plan belonging to the registered target."""

    plan_id: UUIDString


class PrepareInput(TargetInput):
    """Select an adapter action with a stable submission identity."""

    timeout_s: int = Field(default=5, ge=1, le=30)
    action: PlanAction
    parameters: dict[str, Any]
    request_id: UUIDString


class ExecuteInput(PlanInput):
    """Submit one retained plan with a stable submission identity."""

    timeout_s: int = Field(default=5, ge=1, le=30)
    request_id: UUIDString


class ResumeInput(ExecuteInput):
    """Bind a freshly prepared plan to one prior failed or cancelled operation."""

    operation_id: UUIDString


def _outcome(code: str) -> Outcome:
    if code in {
        "invalid_arguments",
        "invalid_input",
        "target_not_found",
        "object_not_found",
        "permission_denied",
        "invalid_cursor",
        "unsupported_contract",
        "request_id_conflict",
        "plan_scope_mismatch",
        "input_missing",
        "plan_not_found",
    }:
        return "invalid_input"
    if code in {
        "stale_plan",
        "adapter_prerequisite_missing",
        "adapter_unavailable",
        "resource_busy",
        "fleet_busy",
        "recovery_required",
        "operation_not_resumable",
        "plan_evidence_missing",
        "plan_evidence_changed",
    }:
        return "failed_gate"
    return "error"


class OperationTools:
    """Map coordinator results without starting work during inspection."""

    def __init__(self, registry: ManagementRegistry, coordinator: OperationCoordinator) -> None:
        self.access = InspectionAccess(registry)
        self.coordinator = coordinator

    async def _invoke(
        self,
        name: str,
        inputs: TargetInput,
        callback: Callable[[], dict[str, Any]],
        *,
        threaded: bool = True,
    ) -> dict[str, Any]:
        audit_target = (
            inputs.target_id
            if any(target.id == inputs.target_id for target in self.access.registry.targets)
            else None
        )
        try:
            self.access.audit(name, audit_target, "started", [])
        except (AccessError, OSError):
            return result(
                name,
                audit_target,
                outcome="invalid_input",
                errors=[
                    {
                        "code": "permission_denied",
                        "message": "Operation audit storage is unavailable or unsafe",
                        "context": {},
                    }
                ],
            )
        try:
            target = self.access.target(inputs.target_id)
            payload = await asyncio.to_thread(callback) if threaded else callback()
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
            if len(encoded) > MAX_RESULT_BYTES:
                reference = ArtifactStore(str(self.access.registry.registry_id), target).export(
                    encoded, kind="management-result"
                )
                payload = result(
                    name,
                    target.id,
                    outcome="degraded",
                    data={"result_artifact_id": reference["artifact_id"]},
                    artifacts=[reference],
                    errors=[
                        {
                            "code": "result_too_large",
                            "message": "Read the retained result artifact",
                            "context": {},
                        }
                    ],
                )
        except (OperationError, AccessError, ArtifactError) as error:
            payload = result(
                name,
                None if error.code == "target_not_found" else inputs.target_id,
                outcome=_outcome(error.code),
                errors=[{"code": error.code, "message": error.message, "context": {}}],
            )
        except ContractVersionError:
            payload = result(
                name,
                inputs.target_id,
                outcome="invalid_input",
                errors=[
                    {
                        "code": "unsupported_contract",
                        "message": "Document version is unsupported",
                        "context": {},
                    }
                ],
            )
        except asyncio.CancelledError:
            with contextlib.suppress(AccessError, OSError):
                self.access.audit(name, audit_target, "interrupted", ["stage_cancelled"])
            execution_event(
                self.access.registry,
                "tool_finished",
                source="mcp",
                tool=name,
                target_id=audit_target,
                action=getattr(inputs, "action", None),
                operation=inputs.model_dump(),
                outcome="interrupted",
                codes=["stage_cancelled"],
                required=False,
            )
            raise
        except OSError:
            payload = result(
                name,
                audit_target,
                outcome="error",
                errors=[
                    {
                        "code": "source_unavailable",
                        "message": "Operation storage is unavailable",
                        "context": {},
                    }
                ],
            )
        except Exception:
            payload = result(
                name,
                audit_target,
                outcome="error",
                errors=[
                    {
                        "code": "adapter_failed",
                        "message": "Operation handler failed to return a valid result",
                        "context": {},
                    }
                ],
            )
        codes = [row["code"] for row in payload["errors"]]
        execution_event(
            self.access.registry,
            "tool_finished",
            source="mcp",
            tool=name,
            target_id=audit_target,
            action=getattr(inputs, "action", None),
            operation={**inputs.model_dump(), **(payload.get("data") or {})},
            outcome=payload["outcome"],
            codes=codes,
            artifacts=payload["artifacts"],
            decision="deny"
            if "permission_denied" in codes or "target_not_found" in codes
            else None,
            required=False,
        )
        try:
            self.access.audit(
                name, audit_target, payload["outcome"], [row["code"] for row in payload["errors"]]
            )
        except (AccessError, OSError):
            payload["outcome"] = "error"
            payload["errors"].append(
                {
                    "code": "audit_failed",
                    "message": "Operation receipt could not be retained",
                    "context": {},
                }
            )
        return payload

    @staticmethod
    def _view(name: str, target_id: str, view: View) -> dict[str, Any]:
        return result(name, target_id, data=view.data, artifacts=view.artifacts)

    @staticmethod
    def _receipt(name: str, target_id: str, operation: dict[str, Any]) -> dict[str, Any]:
        return result(
            name, target_id, outcome="accepted", data={"operation_id": operation["operation_id"]}
        )

    async def _list(self, inputs: OperationListInput) -> dict[str, Any]:
        return await self._invoke(
            "operation_list",
            inputs,
            lambda: self._view(
                "operation_list",
                inputs.target_id,
                self.coordinator.list_operations(inputs.target_id, inputs.limit, inputs.cursor),
            ),
        )

    async def _inspect(self, inputs: OperationInput) -> dict[str, Any]:
        return await self._invoke(
            "operation_inspect",
            inputs,
            lambda: self._view(
                "operation_inspect",
                inputs.target_id,
                self.coordinator.inspect_operation(inputs.target_id, inputs.operation_id),
            ),
        )

    async def _cancel(self, inputs: OperationInput) -> dict[str, Any]:
        return await self._invoke(
            "operation_cancel",
            inputs,
            lambda: self._view(
                "operation_cancel",
                inputs.target_id,
                self.coordinator.cancel_operation(inputs.target_id, inputs.operation_id),
            ),
        )

    async def _plan(self, inputs: PlanInput) -> dict[str, Any]:
        return await self._invoke(
            "plan_inspect",
            inputs,
            lambda: self._view(
                "plan_inspect",
                inputs.target_id,
                self.coordinator.inspect_plan(inputs.target_id, inputs.plan_id),
            ),
        )

    async def _prepare(self, inputs: PrepareInput) -> dict[str, Any]:
        return await self._invoke(
            "plan_prepare",
            inputs,
            lambda: self._receipt(
                "plan_prepare",
                inputs.target_id,
                self.coordinator.submit_prepare(
                    inputs.target_id, inputs.action, inputs.parameters, inputs.request_id
                ),
            ),
            threaded=False,
        )

    async def _execute(self, inputs: ExecuteInput) -> dict[str, Any]:
        return await self._invoke(
            "plan_execute",
            inputs,
            lambda: self._receipt(
                "plan_execute",
                inputs.target_id,
                self.coordinator.submit_execute(
                    inputs.target_id, inputs.plan_id, inputs.request_id
                ),
            ),
            threaded=False,
        )

    async def _resume(self, inputs: ResumeInput) -> dict[str, Any]:
        return await self._invoke(
            "operation_resume",
            inputs,
            lambda: self._receipt(
                "operation_resume",
                inputs.target_id,
                self.coordinator.resume(
                    inputs.target_id, inputs.operation_id, inputs.plan_id, inputs.request_id
                ),
            ),
            threaded=False,
        )

    def adapters(self) -> tuple[ToolAdapter[Any], ...]:
        """Advertise submission only when an execution adapter is installed."""
        tools: tuple[ToolAdapter[Any], ...] = (
            ToolAdapter(
                "operation_list",
                "List retained operation summaries",
                OperationListInput,
                self._list,
            ),
            ToolAdapter(
                "operation_inspect",
                "Read an operation and its frozen record",
                OperationInput,
                self._inspect,
            ),
            ToolAdapter(
                "operation_cancel",
                "Record an authorised cancellation request",
                OperationInput,
                self._cancel,
                read_only=False,
                destructive=True,
            ),
            ToolAdapter(
                "plan_inspect", "Read a retained plan and its input exports", PlanInput, self._plan
            ),
        )
        if self.coordinator.adapters:
            tools += (
                ToolAdapter(
                    "plan_prepare",
                    "Prepare an adapter action and retain its inputs",
                    PrepareInput,
                    self._prepare,
                    read_only=False,
                    open_world=True,
                ),
                ToolAdapter(
                    "plan_execute",
                    "Submit a prepared plan for execution",
                    ExecuteInput,
                    self._execute,
                    read_only=False,
                    destructive=True,
                    open_world=True,
                ),
                ToolAdapter(
                    "operation_resume",
                    "Submit a fresh plan for a reconciled prior operation",
                    ResumeInput,
                    self._resume,
                    read_only=False,
                    destructive=True,
                    open_world=True,
                ),
            )
        return tools


def operation_adapters(
    registry: ManagementRegistry, coordinator: OperationCoordinator
) -> tuple[ToolAdapter[Any], ...]:
    """Build the catalogue for the coordinator's installed execution adapters."""
    return OperationTools(registry, coordinator).adapters()
