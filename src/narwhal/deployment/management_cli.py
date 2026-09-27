"""Prevent registry-bound CLI work from bypassing the management coordinator."""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from narwhal import command_results

from .management_registry import ManagementTarget, PlanAction, load_registry


def _input_path(target: ManagementTarget, *, instance: bool) -> Path | None:
    if instance:
        return target.instance_dir if target.kind == "dev" else None
    if target.kind == "dev":
        return target.instance_dir / "fleet.json" if target.instance_dir is not None else None
    return target.fleet_file


def operation_error_status(code: str) -> str:
    """Map a management failure to the finite command result contract."""
    if code in {"invalid_input", "target_not_found", "permission_denied", "input_missing"}:
        return "invalid_input"
    if code == "stage_cancelled":
        return "interrupted"
    if code in {"stage_timeout", "operation_timeout"}:
        return "error"
    return "failed_gate"


def guard_bound_command(
    command: str,
    *,
    action: PlanAction | None = None,
    fleet: Path | None = None,
    instance: Path | None = None,
    arguments: Mapping[str, Any] | None = None,
) -> int | None:
    """Submit registered dev work or join an authenticated parent operation.

    Unsupported fleet actions fail before their existing callbacks run.
    Supplying an operation ID in the environment cannot authorize a nested call.
    """
    if "NARWHAL_MANAGEMENT_REGISTRY" not in os.environ:
        return None
    from .management_access import AccessError
    from .management_context import authorize_command
    from .management_coordinator import OperationCoordinator
    from .management_records import OperationError

    try:
        if authorize_command(
            command, action=action, fleet=fleet, instance=instance, arguments=arguments
        ):
            return None
        if action is None:
            raise OperationError(
                "adapter_unavailable", "This command has no installed management adapter"
            )
        selected = os.environ["NARWHAL_MANAGEMENT_REGISTRY"]
        if not selected:
            raise OperationError("invalid_input", "Management registry selection is empty")
        registry_path = Path(selected)
        try:
            registry = load_registry(registry_path)
            requested = instance if instance is not None else fleet
            if requested is None:
                raise ValueError("missing target input")
            canonical = requested.expanduser().resolve()
            matches = [
                target
                for target in registry.targets
                if (path := _input_path(target, instance=instance is not None)) is not None
                and path.resolve() == canonical
            ]
        except (OSError, ValueError, RuntimeError):
            raise OperationError(
                "invalid_input", "Management registry or command target path is invalid"
            ) from None
        if not matches:
            raise OperationError("target_not_found", "Command target is not registered")
        if len(matches) != 1:
            raise OperationError("invalid_input", "Command target matches multiple registrations")
        target = matches[0]
        coordinator = OperationCoordinator(registry, registry_path=registry_path)
        coordinator.authorize_action(target.id, action)
        coordinator.adapter(target)
        if command == "narwhal" and action.startswith("dev_"):
            parameters = _dev_parameters(target, action, arguments or {})
            prepared = coordinator.submit_prepare(target.id, action, parameters, str(uuid4()))
            completed = _wait(coordinator, target.id, prepared["operation_id"])
            if completed["state"] != "succeeded":
                return _return_operation(completed, registry=coordinator.registry)
            operation = coordinator.submit_execute(
                target.id, completed["result"]["data"]["plan_id"], str(uuid4())
            )
            return _return_operation(
                _wait(coordinator, target.id, operation["operation_id"]),
                registry=coordinator.registry,
            )
        raise OperationError(
            "adapter_unavailable", "This command has no installed management CLI adapter"
        )
    except (OperationError, AccessError) as error:
        status = operation_error_status(error.code)
        command_results.set_status(status)
        command_results.record_error(error.code, error.message)
        print(f"{command}: {error.code}: {error.message}", file=sys.stderr)
        return command_results.EXIT_CODES[status]


def _dev_parameters(
    target: ManagementTarget, action: str, arguments: Mapping[str, Any]
) -> dict[str, Any]:
    """Match explicit CLI inputs to the registered recipe and initialization settings."""
    if action != "dev_init":
        return {}
    from narwhal.dev.management_settings import resolve_init

    from .management_records import OperationError

    selected = arguments.get("template")
    recipes = [
        recipe
        for recipe in target.recipes
        if selected is None or recipe.path.resolve() == Path(selected).expanduser().resolve()
    ]
    if len(recipes) != 1:
        raise OperationError(
            "invalid_input", "Dev initialization requires one matching registered recipe"
        )
    recipe = recipes[0]
    spec, overrides = resolve_init(target, recipe.id)
    from .management_access import read_object

    saved = (
        read_object(target.instance_dir / "instance.json")
        if target.instance_dir is not None and target.instance_dir.exists()
        else {}
    )
    saved_template = (
        read_object(target.instance_dir / "template.json")
        if saved and target.instance_dir is not None
        else spec
    )
    model = spec["model"]
    hub = Path.home() / ".cache/huggingface/hub"
    resolved = {
        "model_path": saved.get(
            "model_path",
            hub
            / ("models--" + model["repository"].replace("/", "--"))
            / "snapshots"
            / model["revision"]
            / model["filename"],
        ),
        "model_dir": saved.get(
            "model_dir",
            hub
            / ("models--" + model["tokenizer_repository"].replace("/", "--"))
            / "snapshots"
            / model["tokenizer_revision"],
        ),
        "gpu_uuid": saved.get("gpu_uuid"),
        "fabric_interface": saved.get("fabric_interface", "eth0"),
        "engine_count": saved_template["allocation"]["engine_count"],
        "port_base": saved_template["ports"]["router"],
        "gpu_memory_utilization": saved_template["allocation"]["gpu_memory_utilization"],
        "device_allowance": saved_template["allocation"]["device_allowance"],
        **overrides,
    }
    names = {
        "model": "model_path",
        "model_dir": "model_dir",
        "gpu": "gpu_uuid",
        "interface": "fabric_interface",
        "engine_count": "engine_count",
        "port_base": "port_base",
        "gpu_memory_utilization": "gpu_memory_utilization",
        "device_allowance": "device_allowance",
    }
    for name, key in names.items():
        supplied = arguments.get(name)
        if supplied is None:
            continue
        expected = resolved.get(key)
        if isinstance(supplied, Path):
            supplied = supplied.expanduser().absolute()
            expected = Path(expected).expanduser().absolute() if expected is not None else None
        if supplied != expected:
            raise OperationError(
                "invalid_input",
                f"Explicit --{name.replace('_', '-')} differs from registered settings",
            )
    return {"recipe_id": recipe.id}


def _wait(coordinator: Any, target_id: str, operation_id: str) -> dict[str, Any]:
    """Wait for retained work, requesting cancellation if the CLI is interrupted."""
    from .management_executor import worker_alive
    from .management_records import TERMINAL, OperationError

    interrupted = False
    while True:
        try:
            coordinator._target(target_id)
            record = coordinator.store.read(target_id, operation_id)
            if record["state"] in {"running", "cancelling"} and not worker_alive(record["worker"]):
                coordinator.inspect_operation(target_id, operation_id)
                record = coordinator.store.read(target_id, operation_id)
            if record["state"] in TERMINAL:
                return record
            if record["state"] == "recovery_required":
                raise OperationError(
                    "recovery_required", f"Operation {operation_id} requires recovery"
                )
            time.sleep(0.1)
        except KeyboardInterrupt:
            if interrupted:
                raise
            interrupted = True
            coordinator.cancel_operation(target_id, operation_id)


def _return_operation(record: dict[str, Any], *, registry: Any = None) -> int:
    """Return the retained dev command outcome without changing its artifact states."""
    outcome = record["result"]
    command = outcome.get("command_result")
    if command is not None:
        command_results.adopt_result(command)
        if not command_results.json_mode():
            from .management_context import completed_command_exit

            if registry is None:
                from .management_records import OperationError

                raise OperationError("invalid_input", "Managed command completion is unavailable")
            code = completed_command_exit(registry, record)
            print(json.dumps(command["data"], indent=2))
            return code
        return int(command["exit_code"])
    command_results.set_status(outcome["status"])
    command_results.set_data({"operation_id": record["operation_id"]})
    for error in outcome["errors"]:
        command_results.record_error(error["code"], error["message"], context=error.get("context"))
    return command_results.EXIT_CODES[outcome["status"]]
