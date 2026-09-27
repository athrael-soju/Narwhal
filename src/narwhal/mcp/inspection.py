"""Expose registered inspection operations through typed MCP adapters."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pydantic import Field

from narwhal.contracts import FLEET, ContractVersionError, validate_document
from narwhal.deployment.management_access import (
    AccessError,
    InspectionAccess,
    clean_command,
    open_input,
    parse_object,
    read_input,
    read_object,
)
from narwhal.deployment.management_commands import CommandError, run_command
from narwhal.deployment.management_listing import list_targets
from narwhal.deployment.management_registry import ManagementRegistry, ManagementTarget
from narwhal.diagnostics.management_artifacts import ArtifactError, ArtifactStore
from narwhal.diagnostics.management_status import observe_router

from .adapters import MAX_RESULT_BYTES, ToolAdapter, ToolInput
from .results import Alias, UUIDString, from_command_result, result


class TargetInput(ToolInput):
    """Select a registered target without supplying a path or endpoint."""

    target_id: Alias


class ListInput(ToolInput):
    """Select a page from an immutable registration snapshot."""

    limit: int = Field(default=20, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=128)


class DiagnosticInput(TargetInput):
    """Require an explicit request to export permitted request content."""

    include_request_content: bool = False


class ArtifactInput(TargetInput):
    """Read a bounded UTF-8 slice from a previously exported artifact."""

    artifact_id: UUIDString
    offset: int = Field(default=0, ge=0)
    max_bytes: int = Field(default=65_536, ge=1, le=65_536)


def _encoded(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")


def _error(code: str, message: str, **context: Any) -> dict[str, Any]:
    return {"code": code, "message": message, "context": context}


class InspectionTools:
    """Map core observations to bounded management results and audit receipts."""

    def __init__(self, registry: ManagementRegistry) -> None:
        self.access = InspectionAccess(registry)

    def _store(self, target: ManagementTarget) -> ArtifactStore:
        return ArtifactStore(str(self.access.registry.registry_id), target)

    def _bounded(self, payload: dict[str, Any], target: ManagementTarget) -> dict[str, Any]:
        encoded = _encoded(payload)
        if len(encoded) <= MAX_RESULT_BYTES:
            return payload
        reference = self._store(target).export(encoded, kind="management-result")
        replacement = result(
            payload["tool"],
            target.id,
            data={"result_artifact_id": reference["artifact_id"]},
            artifacts=[reference],
            errors=[_error("result_too_large", "Read the retained result artifact")],
        )
        replacement["outcome"] = (
            "degraded" if payload["outcome"] == "success" else payload["outcome"]
        )
        return replacement

    async def _invoke(
        self,
        name: str,
        inputs: ToolInput,
        operation: Callable[[Any, ManagementTarget | None], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        target_id = getattr(inputs, "target_id", None)
        audit_target = (
            target_id
            if any(target.id == target_id for target in self.access.registry.targets)
            else None
        )
        try:
            self.access.audit(name, audit_target, "started", [])
        except (AccessError, OSError):
            return result(
                name,
                target_id,
                outcome="invalid_input",
                errors=[
                    _error(
                        "permission_denied", "Inspection audit storage is unavailable or unsafe"
                    ),
                ],
            )
        try:
            target = self.access.target(target_id) if target_id is not None else None
            payload = await operation(inputs, target)
            if target is not None:
                payload = self._bounded(payload, target)
        except (AccessError, ArtifactError, CommandError) as exc:
            invalid = exc.code in {
                "permission_denied",
                "target_not_found",
                "invalid_input",
                "invalid_cursor",
                "input_missing",
            }
            payload = result(
                name,
                None if exc.code == "target_not_found" else target_id,
                outcome=(
                    "failed_gate"
                    if exc.code == "recovery_required"
                    else "invalid_input"
                    if invalid
                    else "error"
                ),
                errors=[_error(exc.code, exc.message)],
            )
        except ContractVersionError:
            payload = result(
                name,
                target_id,
                outcome="invalid_input",
                errors=[_error("unsupported_contract", "Document version is unsupported")],
            )
        except asyncio.CancelledError:
            with contextlib.suppress(AccessError, OSError):
                self.access.audit(name, audit_target, "interrupted", ["stage_cancelled"])
            raise
        except OSError:
            payload = result(
                name,
                target_id,
                outcome="error",
                errors=[
                    _error("source_unavailable", "Inspection storage is unavailable"),
                ],
            )
        except Exception:
            payload = result(
                name,
                target_id,
                outcome="error",
                errors=[
                    _error("adapter_failed", "Inspection failed to return a valid result"),
                ],
            )
        try:
            self.access.audit(
                name, audit_target, payload["outcome"], [e["code"] for e in payload["errors"]]
            )
        except (AccessError, OSError):
            payload["outcome"] = "error"
            payload["errors"].append(
                _error("audit_failed", "Inspection receipt could not be retained")
            )
        return payload

    async def _listing(self, inputs: ListInput, target: ManagementTarget | None) -> dict[str, Any]:
        return result(
            "target_list",
            data=list_targets(
                self.access.registry,
                limit=inputs.limit,
                cursor=inputs.cursor,
            ),
        )

    async def _config(
        self,
        inputs: TargetInput,
        target: ManagementTarget | None,
        *,
        action: str,
    ) -> dict[str, Any]:
        if target is None:
            raise AccessError("target_not_found", "Target is not registered")
        self.access.check_storage(target)
        with open_input(self.access.fleet_path(target)) as (descriptor, source):
            document = parse_object(source)
            validate_document(document, FLEET)
            redactor = self.access.redactor(target, document)
            command = await run_command(
                ["config", action, "--fleet", f"/proc/self/fd/{descriptor}", "--format", "json"],
                cwd=target.working_directory,
                env=self.access.environment(target, document),
                timeout_s=max(0.1, inputs.timeout_s - 0.5),
                pass_fds=(descriptor,),
            )
        return from_command_result(f"config_{action}", target.id, clean_command(command, redactor))

    async def _inspect(
        self, inputs: TargetInput, target: ManagementTarget | None
    ) -> dict[str, Any]:
        return await self._config(inputs, target, action="inspect")

    async def _validate(
        self, inputs: TargetInput, target: ManagementTarget | None
    ) -> dict[str, Any]:
        return await self._config(inputs, target, action="validate")

    async def _status(self, inputs: TargetInput, target: ManagementTarget | None) -> dict[str, Any]:
        if target is None:
            raise AccessError("target_not_found", "Target is not registered")
        self.access.check_storage(target)
        _, document = self.access.fleet(target)
        rows = await observe_router(
            self.access.router(target),
            timeout_s=max(0.1, inputs.timeout_s - 0.5),
            freshness_s=target.freshness_s,
            redactor=self.access.redactor(target, document),
        )
        artifacts, errors = [], []
        for row in rows:
            if raw := row.pop("raw_body", None):
                reference = self._store(target).export(
                    raw, kind="router-observation", complete=False
                )
                artifacts.append(reference)
                row["artifact_id"] = reference["artifact_id"]
                row["data"] = None
            if code := row.pop("error_code", None):
                errors.append(_error(code, row.pop("error"), source=row["source"]))
        return result(
            "fleet_status",
            target.id,
            outcome=(
                "invalid_input"
                if any(error["code"] == "unsupported_contract" for error in errors)
                else "error"
                if not any(row["data"] is not None or row["artifact_id"] for row in rows)
                else "degraded"
                if errors
                else "success"
            ),
            data={"sources": rows},
            errors=errors,
            artifacts=artifacts,
        )

    async def _dev_status(
        self, inputs: TargetInput, target: ManagementTarget | None
    ) -> dict[str, Any]:
        if target is None:
            raise AccessError("target_not_found", "Target is not registered")
        if target.kind != "dev" or target.instance_dir is None:
            raise AccessError("invalid_input", "Dev status requires a registered dev target")
        self.access.check_storage(target)
        document = read_object(target.instance_dir / "instance.json")
        if document.get("schema") != "narwhal.dev-instance" or document.get("schema_version") != 1:
            raise ContractVersionError("Dev instance version is unsupported")
        expected = Path(document["python_executable"])
        current = Path(sys.executable)
        if expected.parent.resolve() != current.parent.resolve() or not expected.samefile(current):
            raise AccessError(
                "invalid_input", "Run the MCP server in the instance's Python environment"
            )
        from narwhal.deployment.management_records import OperationError
        from narwhal.dev.management_prepare import inspect_ownership

        try:
            ownership = inspect_ownership(target.instance_dir, document)
        except OperationError as error:
            raise AccessError(error.code, error.message) from None
        fleet = {}
        paths = [self.access.fleet_path(target)]
        if ownership["run"] is not None:
            paths.append(Path(ownership["run"]) / "fleet.json")
        for path in paths:
            try:
                source = read_input(path)
            except AccessError as error:
                if error.code != "input_missing":
                    raise
            else:
                try:
                    fleet.update(parse_object(source))
                except AccessError as error:
                    if error.code != "invalid_input":
                        raise
        command = await run_command(
            ["dev", "status", "--instance", str(target.instance_dir), "--format", "json"],
            cwd=target.working_directory,
            env=self.access.environment(target, fleet),
            timeout_s=max(0.1, inputs.timeout_s - 0.5),
        )
        return from_command_result(
            "dev_status", target.id, clean_command(command, self.access.redactor(target, fleet))
        )

    async def _diagnostics(
        self,
        inputs: DiagnosticInput,
        target: ManagementTarget | None,
    ) -> dict[str, Any]:
        from narwhal.diagnostics.management_collection import collect_diagnostics

        if target is None:
            raise AccessError("target_not_found", "Target is not registered")
        collected = await collect_diagnostics(
            self.access,
            target.id,
            timeout_s=inputs.timeout_s,
            include_request_content=inputs.include_request_content,
        )
        payload = from_command_result(
            "diagnostics_collect",
            target.id,
            collected.command_result,
            artifacts=collected.artifacts,
        )
        payload["data"] = collected.data
        payload["errors"].extend(collected.errors)
        if collected.data.get("collection_status") == "partial" and payload["outcome"] == "success":
            payload["outcome"] = "degraded"
        return payload

    async def _artifact(
        self,
        inputs: ArtifactInput,
        target: ManagementTarget | None,
    ) -> dict[str, Any]:
        if target is None:
            raise AccessError("target_not_found", "Target is not registered")
        # JSON escaping can expand a UTF-8 byte to six bytes. A shorter page still
        # satisfies max_bytes and leaves room for the management envelope.
        data = self._store(target).read(
            inputs.artifact_id,
            offset=inputs.offset,
            max_bytes=min(inputs.max_bytes, 32_768),
        )
        return result("artifact_read", target.id, data=data)

    def adapters(self) -> tuple[ToolAdapter[Any], ...]:
        """Return the implemented inspection catalogue."""
        specifications = (
            ("target_list", "List targets that permit inspection", ListInput, self._listing),
            (
                "config_inspect",
                "Read effective registered fleet configuration",
                TargetInput,
                self._inspect,
            ),
            (
                "config_validate",
                "Validate registered fleet configuration offline",
                TargetInput,
                self._validate,
            ),
            (
                "fleet_status",
                "Read bounded router health, readiness and state",
                TargetInput,
                self._status,
            ),
            (
                "dev_status",
                "Read a registered dev instance's lifecycle and current health",
                TargetInput,
                self._dev_status,
            ),
            (
                "diagnostics_collect",
                "Collect and retain bounded registered evidence",
                DiagnosticInput,
                self._diagnostics,
            ),
            (
                "artifact_read",
                "Read a retained redacted artifact by ID",
                ArtifactInput,
                self._artifact,
            ),
        )
        adapters = []
        for name, description, model, operation in specifications:

            async def handler(
                inputs: Any, name: str = name, operation: Any = operation
            ) -> dict[str, Any]:
                return await self._invoke(name, inputs, operation)

            adapters.append(ToolAdapter(name, description, model, handler))
        return tuple(adapters)


def inspection_adapters(registry: ManagementRegistry) -> tuple[ToolAdapter[Any], ...]:
    """Construct the inspection tools without accessing target resources."""
    return InspectionTools(registry).adapters()
