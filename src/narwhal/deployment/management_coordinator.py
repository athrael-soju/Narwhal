"""Coordinate authorised operations through the shared durable deployment store."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from uuid import uuid4

from narwhal import contracts
from narwhal.diagnostics.management_artifacts import ArtifactStore

from .management_access import AccessError, InspectionAccess, parse_object, read_input
from .management_adapters import ManagementAdapter, installed_adapters
from .management_audit import AuditSource, execution_event, operation_event
from .management_exports import public_value, reject_credentials
from .management_plans import PlanStore, canonical, digest, identifier
from .management_records import ACTION_CAPABILITIES, OperationError, encode_record, summary, utc_now
from .management_registry import ManagementRegistry, ManagementTarget, load_registry
from .management_store import OperationStore

if TYPE_CHECKING:
    from .management_executor import StageContext


@dataclass(frozen=True)
class View:
    """Return a public projection and its immutable redacted evidence references."""

    data: dict[str, Any]
    artifacts: list[dict[str, Any]]


def action_parameters(action: str, parameters: dict[str, Any]) -> dict[str, Any]:
    """Reject arbitrary command inputs and normalise unordered engine selections."""
    if action not in ACTION_CAPABILITIES or not isinstance(parameters, dict):
        raise OperationError("invalid_input", "Action or parameters are invalid")
    fields = {
        "dev_init": {"recipe_id"},
        "fleet_deploy": {"recipe_id"},
        "fleet_profile": {"engine_ids"},
        "engine_replace": {"engine_id"},
        "deployment_cleanup": {"operation_id"},
    }.get(action, set())
    if set(parameters) - fields or (action != "fleet_profile" and set(parameters) != fields):
        raise OperationError("invalid_input", "Action parameters do not match their contract")
    resolved = dict(parameters)
    for name in ("recipe_id", "engine_id"):
        if name in resolved and (
            not isinstance(resolved[name], str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", resolved[name])
        ):
            raise OperationError("invalid_input", "Action selector must be a registered alias")
    if "operation_id" in resolved:
        resolved["operation_id"] = identifier(resolved["operation_id"])
    if "engine_ids" in resolved:
        values = resolved["engine_ids"]
        if (
            not isinstance(values, list)
            or not 1 <= len(values) <= 256
            or any(
                not isinstance(value, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value)
                for value in values
            )
            or len(set(values)) != len(values)
        ):
            raise OperationError("invalid_input", "Engine selection must contain distinct aliases")
        resolved["engine_ids"] = sorted(values)
    canonical(resolved)
    return resolved


class OperationCoordinator:
    """Apply current grants before storage access and reserve before worker effects."""

    def __init__(
        self,
        registry: ManagementRegistry,
        registry_path: Path | None = None,
        adapters: Mapping[str, ManagementAdapter] | None = None,
        launcher: Callable[[str, str], None] | None = None,
        *,
        entry_point: AuditSource = "core",
    ) -> None:
        self.registry = registry
        self.registry_path = registry_path
        self.adapters = dict(installed_adapters() if adapters is None else adapters)
        self.store = OperationStore(registry)
        self.launcher = launcher
        self.entry_point = entry_point

    def _access(self) -> InspectionAccess:
        registry = (
            load_registry(self.registry_path) if self.registry_path is not None else self.registry
        )
        if (
            registry.registry_id != self.registry.registry_id
            or registry.state_dir != self.registry.state_dir
        ):
            raise OperationError(
                "permission_denied", "Management authority changed; restart the frontend"
            )
        return InspectionAccess(registry)

    def _target(self, target_id: str) -> ManagementTarget:
        access = self._access()
        try:
            target = access.target(target_id)
        except AccessError as error:
            execution_event(
                access.registry,
                "permission_decision",
                source=self.entry_point,
                target_id=target_id,
                outcome="denied",
                decision="deny",
                inspect_only=True,
                codes=[error.code],
            )
            raise
        store = OperationStore(access.registry)
        store.check_registry(access.registry)
        self.store = store
        return target

    def authorize_action(
        self,
        target_id: str,
        action: str,
        *,
        operation: dict[str, Any] | None = None,
        source: AuditSource | None = None,
    ) -> ManagementTarget:
        """Require separate inspection, measurement, mutation and action grants."""
        access = self._access()
        try:
            target = access.target(target_id)
            required = ACTION_CAPABILITIES.get(action)
            if required is None:
                raise OperationError("invalid_input", "Action is unsupported")
            if action not in target.actions or not required <= set(target.capabilities):
                raise OperationError("permission_denied", "Target does not permit this action")
        except (OperationError, AccessError) as error:
            execution_event(
                access.registry,
                "permission_decision",
                source=source or self.entry_point,
                target_id=target_id,
                action=action,
                operation=operation,
                outcome="denied",
                decision="deny",
                codes=[error.code],
            )
            raise
        execution_event(
            access.registry,
            "permission_decision",
            source=source or self.entry_point,
            target_id=target_id,
            action=action,
            operation=operation,
            outcome="allowed",
            decision="allow",
        )
        # Audit the decision before opening the durable store or admitting work.
        store = OperationStore(access.registry)
        store.check_registry(access.registry)
        self.store = store
        return target

    def adapter(self, target: ManagementTarget) -> ManagementAdapter:
        """Resolve a trusted installed adapter for the registered target."""
        selected = self.adapters.get(target.adapter.id)
        if selected is None:
            raise OperationError(
                "adapter_unavailable", "Execution adapter is not available in this build"
            )
        if selected.manifest.id != target.adapter.id:
            raise OperationError("adapter_unavailable", "Adapter does not match registration")
        return selected

    def _registration_digest(self, target: ManagementTarget) -> str:
        redactor = InspectionAccess(self.registry).redactor(target)
        settings = None
        if target.adapter.settings_path is not None:
            document = parse_object(read_input(target.adapter.settings_path))
            redactor.references(document)
            settings = digest(encode_record(redactor.value(document)))
        endpoints = {}
        for name, reference in target.endpoints.model_dump().items():
            value = os.environ.get(reference) if reference is not None else None
            if reference is not None:
                parsed = urlsplit(value or "")
                if (
                    parsed.scheme not in {"http", "https"}
                    or not parsed.hostname
                    or parsed.username is not None
                    or parsed.password is not None
                    or parsed.query
                    or parsed.fragment
                ):
                    raise OperationError(
                        "invalid_input", "Registered endpoint is missing or invalid"
                    )
            endpoints[name] = value
        recipes = [
            {"id": recipe.id, "sha256": digest(read_input(recipe.path))}
            for recipe in sorted(target.recipes, key=lambda recipe: recipe.id)
        ]
        return digest(
            canonical(
                {
                    "registry_id": str(self.registry.registry_id),
                    "target": target.model_dump(mode="json"),
                    "endpoints": endpoints,
                    "settings_sha256": settings,
                    "recipes": recipes,
                }
            )
        )

    def _check_local_binding(
        self, target: ManagementTarget, plan: dict[str, Any]
    ) -> ManagementAdapter:
        payload = plan["payload"]
        binding = payload["binding"]
        selected = self.adapter(target)
        manifest = selected.manifest
        expected = {
            "id": manifest.id,
            "version": manifest.version,
            "assets_sha256": manifest.assets_sha256,
        }
        if (
            payload["target_id"] != target.id
            or binding["registration_digest"] != self._registration_digest(target)
            or binding["adapter"] != expected
            or binding["source"] != manifest.source
        ):
            raise OperationError("stale_plan", "Registration, adapter or source identity changed")
        required = manifest.actions.get(payload["action"])
        operations = {stage["operation"] for stage in payload["stages"]}
        if required is None or operations != set(required):
            raise OperationError(
                "invalid_input", "Plan does not contain the adapter's required operations"
            )
        recipe = binding["recipe"]
        if recipe is not None:
            registered = next(
                (entry for entry in target.recipes if entry.id == recipe["recipe_id"]), None
            )
            if registered is None or digest(read_input(registered.path)) != recipe["sha256"]:
                raise OperationError("stale_plan", "Registered recipe changed")
        return selected

    def _launch(self, target_id: str, operation: dict[str, Any], created: bool) -> dict[str, Any]:
        if created or operation["state"] == "queued":
            try:
                operation_event(
                    self.registry,
                    "operation_accepted",
                    self.store.read(target_id, operation["operation_id"]),
                    source=self.entry_point,
                    outcome="accepted",
                )
                if self.launcher is not None:
                    self.launcher(target_id, operation["operation_id"])
                elif self.registry_path is not None:
                    from .management_worker import launch_worker

                    launch_worker(self.registry_path, target_id, operation["operation_id"])
                else:
                    raise OperationError(
                        "worker_unavailable", "A registry path is required to start a worker"
                    )
            except (OSError, OperationError):
                # Admission is already durable. A lost launch receipt cannot erase its request key.
                # Competing children must claim a queued record atomically.
                pass
        return operation

    def _replay(
        self,
        target_id: str,
        request_id: str,
        *,
        tool: str,
        plan_id: str | None = None,
        action: str | None = None,
        parameters: dict[str, Any] | None = None,
        parent: str | None = None,
    ) -> dict[str, Any] | None:
        """Recover an accepted request before checking inputs for new work."""
        found = self.store.lookup_request(target_id, request_id)
        same_key = found is not None
        if found is None and plan_id is not None:
            found = self.store.lookup_plan(target_id, plan_id)
        if found is None:
            return None
        self.authorize_action(target_id, found["action"])
        if (
            found["tool"] != tool
            or found["plan_id"] != plan_id
            or found["parent_operation_id"] != parent
            or (action is not None and found["action"] != action)
            or (parameters is not None and found["parameters"] != parameters)
        ):
            raise OperationError(
                "request_id_conflict" if same_key else "plan_scope_mismatch",
                "Submission differs from the retained operation",
            )
        operation = self.store.read(target_id, found["operation_id"])
        if not same_key:
            operation, _ = self.store.admit(
                target_id=target_id,
                request_id=request_id,
                tool=tool,
                action=found["action"],
                parameters=found["parameters"],
                plan=self.store.plan(target_id, operation["operation_id"]),
                parent_operation_id=parent,
                resources=[row["resource_id"] for row in operation["resources"]],
                cleanup_of=(
                    found["parameters"]["operation_id"]
                    if found["action"] == "deployment_cleanup" and tool != "plan_prepare"
                    else None
                ),
            )
        return self._launch(target_id, operation, False)

    def submit_prepare(
        self, target_id: str, action: str, parameters: dict[str, Any], request_id: str
    ) -> dict[str, Any]:
        """Record an authorised preparation before starting its detached worker."""
        target = self.authorize_action(
            target_id, action, operation={"tool": "plan_prepare", "request_id": request_id}
        )
        resolved = action_parameters(action, parameters)
        replay = self._replay(
            target_id, request_id, tool="plan_prepare", action=action, parameters=resolved
        )
        if replay is not None:
            return replay
        selected = self.adapter(target)
        if action not in selected.manifest.actions:
            raise OperationError(
                "adapter_unavailable", "Adapter does not implement the selected action"
            )
        if "recipe_id" in resolved and not any(
            entry.id == resolved["recipe_id"] for entry in target.recipes
        ):
            raise OperationError("invalid_input", "Recipe is not registered")
        operation, created = self.store.admit(
            target_id=target_id,
            request_id=request_id,
            tool="plan_prepare",
            action=action,
            parameters=resolved,
            preparation_budgets=target.preparation.model_dump(),
        )
        return self._launch(target_id, operation, created)

    def submit_execute(self, target_id: str, plan_id: str, request_id: str) -> dict[str, Any]:
        """Reserve the saved plan resources and record one execution."""
        self._target(target_id)
        plan_id = identifier(plan_id)
        replay = self._replay(target_id, request_id, tool="plan_execute", plan_id=plan_id)
        if replay is not None:
            return replay
        plan = PlanStore(self.registry, target_id).read(plan_id)
        target = self.authorize_action(
            target_id,
            plan["payload"]["action"],
            operation={"tool": "plan_execute", "plan_id": plan_id, "request_id": request_id},
        )
        self._check_local_binding(target, plan)
        return self._execute(target, plan, request_id)

    def _execute(
        self,
        target: ManagementTarget,
        plan: dict[str, Any],
        request_id: str,
        parent: str | None = None,
    ) -> dict[str, Any]:
        store = PlanStore(self.registry, target.id)
        snapshot = json.loads(store.blob(plan["payload"]["binding"]["snapshot_sha256"]))
        resources = snapshot["identity"].get("resources")
        if (
            not isinstance(resources, list)
            or not resources
            or len(resources) > 4096
            or any(
                not isinstance(value, str) or not value or len(value) > 512 for value in resources
            )
            or len(set(resources)) != len(resources)
        ):
            raise OperationError(
                "invalid_input", "Adapter snapshot must identify distinct canonical resources"
            )
        operation, created = self.store.admit(
            target_id=target.id,
            request_id=request_id,
            tool="operation_resume" if parent else "plan_execute",
            action=plan["payload"]["action"],
            parameters=plan["payload"]["parameters"],
            plan=plan,
            resources=sorted(resources),
            parent_operation_id=parent,
            cleanup_of=(
                plan["payload"]["parameters"]["operation_id"]
                if plan["payload"]["action"] == "deployment_cleanup"
                else None
            ),
        )
        return self._launch(target.id, operation, created)

    def resume(
        self, target_id: str, operation_id: str, plan_id: str, request_id: str
    ) -> dict[str, Any]:
        """Create a child attempt from a fresh plan after reconciled failure."""
        self._target(target_id)
        operation_id, plan_id = identifier(operation_id), identifier(plan_id)
        replay = self._replay(
            target_id, request_id, tool="operation_resume", plan_id=plan_id, parent=operation_id
        )
        if replay is not None:
            return replay
        parent = self.store.read(target_id, operation_id)
        target = self.authorize_action(
            target_id,
            parent["action"],
            operation={
                "tool": "operation_resume",
                "operation_id": operation_id,
                "plan_id": plan_id,
                "request_id": request_id,
            },
        )
        if parent["state"] == "recovery_required":
            raise OperationError(
                "recovery_required", "Original operation effects remain unresolved"
            )
        if parent["tool"] == "plan_prepare" or parent["state"] not in {"failed", "cancelled"}:
            raise OperationError(
                "operation_not_resumable", "Original operation is not eligible for resumption"
            )
        plan = PlanStore(self.registry, target_id).read(plan_id)
        if plan["payload"]["action"] != parent["action"] or plan["plan_id"] == parent["plan_id"]:
            raise OperationError(
                "plan_scope_mismatch", "Resumption requires a fresh plan for the same action"
            )
        self._check_local_binding(target, plan)
        # Every stage receives a new record. No failed or stale evidence qualifies the child.
        return self._execute(target, plan, request_id, parent["operation_id"])

    def _redact(self, target: ManagementTarget, value: Any) -> Any:
        return public_value(self._access(), target, value)

    def list_operations(self, target_id: str, limit: int = 20, cursor: str | None = None) -> View:
        """Read a page from one immutable summary snapshot."""
        self._target(target_id)
        return View(self.store.list(target_id, limit=limit, cursor=cursor), [])

    def _operation_view(self, target: ManagementTarget, operation: dict[str, Any]) -> View:
        public = self._redact(target, operation)
        reference = ArtifactStore(str(self.registry.registry_id), target).export(
            encode_record(public), kind="management-operation"
        )
        return View(
            {"operation": summary(operation), "record_artifact_id": reference["artifact_id"]},
            [reference],
        )

    def inspect_operation(self, target_id: str, operation_id: str) -> View:
        """Read operation evidence and reconcile lost worker identity."""
        target = self._target(target_id)
        operation = self.store.read(target_id, operation_id)
        if operation["state"] in {"running", "cancelling", "recovery_required"}:
            from .management_executor import reconcile_operation

            operation = reconcile_operation(self.registry, target_id, operation_id)
            self._schedule_reconcile(target, operation)
        return self._operation_view(target, operation)

    def _schedule_reconcile(self, target: ManagementTarget, operation: dict[str, Any]) -> None:
        """Run bounded adapter inspection outside the MCP process and event loop."""
        if (
            operation["state"] == "recovery_required"
            and self.registry_path is not None
            and target.adapter.id in self.adapters
        ):
            from .management_worker import launch_worker

            launch_worker(self.registry_path, target.id, operation["operation_id"], reconcile=True)

    def reconcile_worker(self, target_id: str, operation_id: str) -> dict[str, Any]:
        """Inspect resources under current inspection grants in a detached worker."""
        from .management_executor import reconcile_operation

        target = self._target(target_id)
        return reconcile_operation(
            self.registry, target_id, operation_id, adapter=self.adapters.get(target.adapter.id)
        )

    def cancel_operation(self, target_id: str, operation_id: str) -> View:
        """Persist an idempotent cancellation request under current grants."""
        target = self._target(target_id)
        operation = self.store.read(target_id, operation_id)
        if operation["tool"] != "plan_prepare":
            target = self.authorize_action(
                target_id, operation["action"], operation={**operation, "tool": "operation_cancel"}
            )
        execution_event(
            self._access().registry,
            "cancellation_requested",
            source=self.entry_point,
            tool="operation_cancel",
            target_id=target_id,
            action=operation["action"],
            operation=operation,
            outcome="requested",
            decision="allow",
            inspect_only=operation["tool"] == "plan_prepare",
        )
        operation = self.store.request_cancel(target_id, operation_id)
        if operation["state"] == "queued":
            from .management_executor import cancel_queued_operation

            operation = cancel_queued_operation(self.registry, target_id, operation_id)
        elif operation["state"] in {"cancelling", "recovery_required"}:
            from .management_executor import cancel_recovery_operation

            operation = cancel_recovery_operation(self.registry, target_id, operation_id)
            self._schedule_reconcile(target, operation)
        return self._operation_view(target, operation)

    def reconcile_startup(self) -> None:
        """Inspect saved worker ownership without replaying interrupted effects."""
        from .management_executor import reconcile_operation

        self.store.check_registry(self._access().registry)
        for target in self._access().registry.targets:
            if "inspect" not in target.capabilities:
                continue
            cursor = None
            while True:
                page = self.store.list(target.id, limit=100, cursor=cursor)
                for operation in page["operations"]:
                    if operation["state"] in {"running", "cancelling", "recovery_required"}:
                        retained = reconcile_operation(
                            self.registry, target.id, operation["operation_id"]
                        )
                        self._schedule_reconcile(target, retained)
                    elif operation["state"] == "queued" and target.adapter.id in self.adapters:
                        try:
                            self.authorize_action(target.id, operation["action"])
                            self._launch(target.id, operation, False)
                        except OperationError:
                            continue
                cursor = page["next_cursor"]
                if cursor is None:
                    break

    def inspect_plan(self, target_id: str, plan_id: str) -> View:
        """Export the saved plan and its verified, redacted input evidence."""
        target = self._target(target_id)
        store = PlanStore(self.registry, target_id)
        plan = store.read(plan_id)
        artifacts = ArtifactStore(str(self.registry.registry_id), target)
        references = []
        input_artifacts = []
        for entry in plan["payload"]["binding"]["inputs"]:
            raw = store.blob(entry["sha256"])
            try:
                value = json.loads(raw)
            except (ValueError, UnicodeError):
                try:
                    value = raw.decode("utf-8")
                except UnicodeError:
                    raise OperationError(
                        "unsupported_media_type", "Plan input cannot be exported as text"
                    ) from None
            public = self._redact(target, value)
            data = public.encode() if isinstance(public, str) else encode_record(public)
            reference = artifacts.export(data, kind="management-plan-input")
            references.append(reference)
            input_artifacts.append({"name": entry["name"], "artifact_id": reference["artifact_id"]})
        snapshot = json.loads(store.blob(plan["payload"]["binding"]["snapshot_sha256"]))
        reference = artifacts.export(
            encode_record(self._redact(target, snapshot)), kind="management-snapshot"
        )
        references.append(reference)
        binding = plan["payload"]["binding"]
        locally_stale = binding["registration_digest"] != self._registration_digest(target)
        if target.adapter.id in self.adapters:
            try:
                self._check_local_binding(target, plan)
            except OperationError as error:
                if error.code != "stale_plan":
                    raise
                locally_stale = True
        return View(
            {
                "plan": self._redact(target, plan),
                "locally_stale": locally_stale,
                "input_artifacts": input_artifacts,
                "snapshot_artifact_id": reference["artifact_id"],
            },
            references,
        )

    def prepare_plan(self, context: StageContext, operation: dict[str, Any]) -> dict[str, Any]:
        """Persist discovered inputs before publishing a plan from the detached worker."""
        target = self.authorize_action(
            operation["target_id"], operation["action"], operation=operation, source="worker"
        )
        adapter = self.adapter(target)
        parameters = self.store.request(target.id, operation["operation_id"])["parameters"]
        registration_digest = self._registration_digest(target)
        prepared = adapter.prepare(context, target, operation["action"], parameters)
        context.assert_current()
        if (
            self._registration_digest(
                self.authorize_action(
                    target.id, operation["action"], operation=context.read(), source="worker"
                )
            )
            != registration_digest
        ):
            raise OperationError("stale_plan", "Registration changed during preparation")
        resolved = action_parameters(operation["action"], prepared.parameters)
        if any(resolved.get(key) != value for key, value in parameters.items()):
            raise OperationError("invalid_input", "Adapter changed explicit action parameters")
        retained = [
            encode_record(prepared.identity),
            encode_record(prepared.observations),
            encode_record({"stages": prepared.stages}),
            *prepared.inputs.values(),
        ]
        reject_credentials(self._access(), target, retained)
        store = PlanStore(self.registry, target.id)
        snapshot = contracts.versioned(
            contracts.MANAGEMENT_SNAPSHOT,
            {
                "snapshot_id": str(uuid4()),
                "observed_at": utc_now(),
                "identity": prepared.identity,
                "observations": prepared.observations,
            },
        )
        recipe = None
        if "recipe_id" in resolved:
            entry = next(
                (item for item in target.recipes if item.id == resolved["recipe_id"]), None
            )
            if entry is None:
                raise OperationError("invalid_input", "Recipe is not registered")
            recipe = {"recipe_id": entry.id, "sha256": digest(read_input(entry.path))}
        manifest = adapter.manifest
        payload = {
            "action": operation["action"],
            "target_id": target.id,
            "parameters": resolved,
            "stages": prepared.stages,
            "binding": {
                "registration_digest": registration_digest,
                "adapter": {
                    "id": manifest.id,
                    "version": manifest.version,
                    "assets_sha256": manifest.assets_sha256,
                },
                "source": manifest.source,
                "recipe": recipe,
                "snapshot_id": snapshot["snapshot_id"],
                "snapshot_sha256": store.put_blob(encode_record(snapshot)),
                "identity_sha256": digest(canonical(prepared.identity)),
                "inputs": [
                    {"name": name, "sha256": store.put_blob(data)}
                    for name, data in sorted(prepared.inputs.items())
                ],
            },
        }
        plan = store.save(payload)
        self._check_local_binding(target, plan)
        return {"plan_id": plan["plan_id"]}

    def run_worker(self, target_id: str, operation_id: str) -> dict[str, Any]:
        """Run the admitted action with current permissions at each stage."""
        from .management_executor import run_operation

        target = self._target(target_id)
        operation = self.store.read(target_id, operation_id)
        adapter = self.adapters.get(target.adapter.id)
        plan = self.store.plan(target_id, operation_id)

        def check(context: StageContext) -> None:
            current = self.authorize_action(
                target_id, operation["action"], operation=context.read(), source="worker"
            )
            selected = self.adapter(current)
            if plan is not None:
                PlanStore(self.registry, target_id).read(plan["plan_id"])
                self._check_local_binding(current, plan)
                if not context.read_only:
                    selected.check(context, current, plan)

        return run_operation(
            self.registry,
            target_id,
            operation_id,
            adapter=adapter,
            plan=plan,
            prepare=self.prepare_plan,
            before_start=check,
            before_stage=check,
        )


def run_worker(registry_path: Path, target_id: str, operation_id: str) -> dict[str, Any]:
    """Load the authority used by the fixed detached worker entry point."""
    registry = load_registry(registry_path)
    return OperationCoordinator(registry, registry_path, entry_point="worker").run_worker(
        target_id, operation_id
    )
