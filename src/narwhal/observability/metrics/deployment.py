"""Render the deployment status document and this process's restart count on `/metrics`.

`narwhal deploy run` writes the document, `narwhal.deployment-status`, to each router host.
Every router of a pair exports the same document, so the dashboard counts each of its
series once per fleet.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, TypeGuard

from ...contracts import DEPLOYMENT_STATUS, validate_document
from .exposition import metric_lines

STATES = ("passed", "failed", "running", "cancelled", "timeout", "invalidated")


def read_status(path: Path) -> dict[str, Any] | None:
    """The document at `path`, or None when there is none or it is unreadable."""
    try:
        document = json.loads(path.read_text())
        validate_document(document, DEPLOYMENT_STATUS)
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def _time(value: Any) -> TypeGuard[int | float]:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def render_deployment(status: dict[str, Any] | None) -> list[str]:
    """The run's stage states, its times, the fabric edges and the attested engines."""
    if status is None:
        return []
    out = metric_lines(
        "narwhal_deployment_info",
        "The deployment run this router last received the status of",
        "gauge",
        [({"run_id": str(status.get("run_id", "")), "target": str(status.get("target", ""))}, 1)],
    )
    out += metric_lines(
        "narwhal_deployment_stage_state",
        "1 for the state of each stage instance of the last deployment run",
        "gauge",
        [
            (
                {
                    "stage": str(stage["stage"]),
                    "stage_instance": str(stage["instance"]),
                    "state": str(stage["state"]),
                },
                1,
            )
            for stage in status.get("stages", [])
        ],
    )
    for name, field, text in (
        ("updated", "updated_at", "When the last deployment run last changed a stage"),
        ("last_passed", "last_passed_at", "When a deployment run last passed every stage"),
        ("qualified", "qualified_at", "When preflight last passed for the fleet"),
        ("profiled", "profiled_at", "When the fleet's engines were last profiled"),
        ("calibrated", "calibrated_at", "When the first-token deadline was last calibrated"),
    ):
        value = status.get(field)
        out += metric_lines(
            f"narwhal_deployment_{name}_timestamp_seconds",
            text,
            "gauge",
            [({}, float(value))] if _time(value) else [],
        )
    edges = status.get("fabric", [])
    out += metric_lines(
        "narwhal_fabric_edge_measured_gbps",
        "Bandwidth fabric qualification measured on each directed edge, in Gbit/s",
        "gauge",
        [({"edge": str(edge["edge"])}, edge["measured_gbps"]) for edge in edges],
    )
    out += metric_lines(
        "narwhal_fabric_edge_budget_gbps",
        "Bandwidth each directed edge must carry for its sources' KV handoffs, in Gbit/s",
        "gauge",
        [({"edge": str(edge["edge"])}, edge["budget_gbps"]) for edge in edges],
    )
    engines = status.get("engines", [])
    out += metric_lines(
        "narwhal_engine_attestation_generation_info",
        "1 for the attested binding digest of each engine",
        "gauge",
        [({"iid": str(e["iid"]), "generation": str(e["generation"])}, 1) for e in engines],
    )
    out += metric_lines(
        "narwhal_engine_attested_timestamp_seconds",
        "When each engine's attestation was last captured",
        "gauge",
        [({"iid": str(e["iid"])}, e["attested_at"]) for e in engines if _time(e["attested_at"])],
    )
    return out
