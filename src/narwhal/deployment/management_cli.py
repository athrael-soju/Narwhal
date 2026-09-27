"""Prevent registry-bound CLI work from bypassing the management coordinator."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from narwhal import command_results

from .management_registry import ManagementTarget, PlanAction, load_registry


def _input_path(target: ManagementTarget, *, instance: bool) -> Path | None:
    if instance:
        return target.instance_dir if target.kind == "dev" else None
    if target.kind == "dev":
        return target.instance_dir / "fleet.json" if target.instance_dir is not None else None
    return target.fleet_file


def guard_bound_command(
    command: str,
    *,
    action: PlanAction | None = None,
    fleet: Path | None = None,
    instance: Path | None = None,
) -> int | None:
    """Leave unbound commands unchanged and reject unsupported managed execution.

    Finite CLI adapters will replace this rejection with coordinated submission.
    Merely supplying an operation ID in the environment never bypasses the guard.
    """
    if "NARWHAL_MANAGEMENT_REGISTRY" not in os.environ:
        return None
    from .management_access import AccessError
    from .management_coordinator import OperationCoordinator
    from .management_records import OperationError

    try:
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
        raise OperationError(
            "adapter_unavailable", "This command has no installed management CLI adapter"
        )
    except (OperationError, AccessError) as error:
        status = (
            "invalid_input"
            if error.code in {"invalid_input", "target_not_found", "permission_denied"}
            else "failed_gate"
        )
        command_results.set_status(status)
        command_results.record_error(error.code, error.message)
        print(f"{command}: {error.code}: {error.message}", file=sys.stderr)
        return command_results.EXIT_CODES[status]
