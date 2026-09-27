"""Declare the required order and finite budgets of SSH deployment stages."""

from __future__ import annotations

from typing import Any

from .management_records import OperationError
from .ssh_settings import SSHSettings

# Each row fixes an operation, its deadline class and any retained services.
_DEPLOY = (
    ("A", "fleet.discover", "discovery", ()),
    ("B", "fleet.install", "installation", ("installation",)),
    ("C", "fleet.launch", "engines", ("engine",)),
    ("D", "fleet.fabric", "fabric", ()),
    ("E", "fleet.attest", "attestation", ("attestation",)),
    ("F", "fleet.profile", "profiling", ()),
    ("F", "fleet.preflight", "preflight", ()),
    ("G", "fleet.serve", "serving", ("router",)),
    ("G", "fleet.monitor", "serving", ("monitoring",)),
    ("G", "fleet.workload", "workload", ()),
    ("G", "fleet.postload", "preflight", ()),
    ("G", "fleet.accept", "serving", ()),
)
_REPLACE = (
    (None, "fleet.drain", "serving", ()),
    (None, "fleet.stop_engine", "cleanup", ()),
    (None, "fleet.launch", "engines", ("engine",)),
    (None, "fleet.fabric", "fabric", ()),
    (None, "fleet.attest", "attestation", ("attestation",)),
    (None, "fleet.profile", "profiling", ()),
    (None, "fleet.preflight", "preflight", ()),
    (None, "fleet.activate_profiles", "serving", ("router",)),
    (None, "fleet.readmit", "serving", ()),
    (None, "fleet.workload", "workload", ()),
    (None, "fleet.postload", "preflight", ()),
    (None, "fleet.accept", "serving", ()),
)


def action_operations() -> dict[str, frozenset[str]]:
    """List the exact implementation operations allowed for each plan action."""
    return {
        "fleet_deploy": frozenset(row[1] for row in _DEPLOY),
        "engine_replace": frozenset(row[1] for row in _REPLACE),
        "fleet_profile": frozenset({"fleet.profile"}),
        "fleet_preflight": frozenset({"fleet.preflight"}),
        "monitoring_start": frozenset({"fleet.monitor"}),
        "deployment_cleanup": frozenset({"fleet.cleanup"}),
    }


def stages(
    action: str,
    settings: SSHSettings,
    *,
    subjects: list[str],
    input_names: list[str],
) -> list[dict[str, Any]]:
    """Require every gate, including workload acceptance, before deployment success."""
    rows: tuple[tuple[str | None, str, str, tuple[str, ...]], ...]
    if action == "fleet_deploy":
        rows = _DEPLOY
    elif action == "engine_replace":
        rows = _REPLACE
    else:
        selection = {
            "fleet_profile": ("fleet.profile", "profiling", ()),
            "fleet_preflight": ("fleet.preflight", "preflight", ()),
            "monitoring_start": ("fleet.monitor", "serving", ("monitoring",)),
            "deployment_cleanup": ("fleet.cleanup", "cleanup", ()),
        }.get(action)
        if selection is None:
            raise OperationError("invalid_input", "Unsupported SSH deployment action")
        operation, budget, retained = selection
        rows = ((None, operation, budget, retained),)
    result: list[dict[str, Any]] = []
    for gate, operation, budget_name, retained in rows:
        budget = getattr(settings.budgets, budget_name)
        stage_id = operation.replace(".", "-")
        result.append(
            {
                "stage_id": stage_id,
                "gate": gate,
                "operation": operation,
                "depends_on": [result[-1]["stage_id"]] if result else [],
                "subjects": subjects,
                "input_names": sorted(input_names),
                "timeout_ms": budget.timeout_ms,
                "cleanup": {
                    "policy": "temporary_only",
                    "term_grace_ms": budget.term_grace_ms,
                    "kill_grace_ms": budget.kill_grace_ms,
                    "reconcile_ms": budget.reconcile_ms,
                },
                "retain_on_success": list(retained),
            }
        )
    return result
