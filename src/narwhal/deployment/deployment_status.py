"""The deployment status document that `narwhal deploy run` gives each router to export.

After each run, passed or not, the workstation builds the document from the run record and
uploads it to every router host as `runs/deployment/deployment-status.json` in the
deployment directory. Each router reads it on every scrape of `/metrics`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..contracts import DEPLOYMENT_STATUS, versioned
from .run_record import PASSED

# Where each router host keeps the document, under its deployment directory.
HOST_PATH = "runs/deployment/deployment-status.json"
# The workstation's copy, beside the run record, which keeps the last passed run's time.
LOCAL_NAME = "deployment-status.json"
# The edge results that `fabric-edge` keeps in its attempt directory.
EDGE_RESULTS = "edge-results.json"


def _ended(entry: Mapping[str, Any] | None) -> float | None:
    if entry is None or entry["status"] != PASSED:
        return None
    ended = entry.get("ended_at")
    return float(ended) if isinstance(ended, int | float) else None


def build(
    record: Mapping[str, Any],
    directory: Path,
    order: Sequence[str],
    last_stage: str,
    fleet: str,
    target: str,
    previous: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The status of the run in `directory`, whose stages run in `order` up to `last_stage`."""
    covered = set(order[: order.index(last_stage) + 1])
    entries = [entry for entry in record["stages"].values() if entry["stage"] in covered]
    stages = [
        {"stage": entry["stage"], "instance": entry["instance"], "state": entry["status"]}
        for entry in sorted(
            entries, key=lambda entry: (order.index(entry["stage"]), entry["instance"])
        )
    ]
    # A stage of a capability no engine has has no instance, so the run passed once the last
    # stage did and every recorded stage up to it passed.
    ran = {entry["stage"] for entry in entries}
    passed = last_stage in ran and all(entry["status"] == PASSED for entry in entries)
    ends = [_ended(entry) for entry in entries]
    last_passed = (previous or {}).get("last_passed_at")
    if passed and all(end is not None for end in ends):
        last_passed = max(end for end in ends if end is not None)

    def latest(stage: str) -> float | None:
        times = [_ended(entry) for entry in entries if entry["stage"] == stage]
        found = [time for time in times if time is not None]
        return max(found) if found else None

    fabric = []
    engines = []
    for entry in entries:
        if entry["status"] != PASSED:
            continue
        if entry["stage"] == "fabric-edge":
            results = directory / entry["directory"] / EDGE_RESULTS
            if not results.is_file():
                continue
            samples = json.loads(results.read_text())
            measured = [s["measured_gbps"] for s in samples]
            budgets = [r["required_gbps"] for s in samples for r in s["roles"].values()]
            if measured and budgets:
                fabric.append(
                    {
                        "edge": entry["instance"],
                        "measured_gbps": min(measured),
                        "budget_gbps": max(budgets),
                    }
                )
        elif entry["stage"] == "attest-capture":
            binding = entry["products"].get("binding")
            if isinstance(binding, str):
                engines.append(
                    {
                        "iid": "n" + entry["instance"].rsplit("-", 1)[1],
                        "generation": binding,
                        "attested_at": _ended(entry),
                    }
                )
    body = {
        "fleet": fleet,
        "target": target,
        "run_id": record["run_id"],
        "updated_at": max(
            [e.get("ended_at") or e.get("started_at") or 0.0 for e in entries] or [0.0]
        ),
        "last_passed_at": last_passed,
        "qualified_at": latest("preflight"),
        "profiled_at": latest("profile"),
        "calibrated_at": latest("calibrate"),
        "stages": stages,
        "fabric": sorted(fabric, key=lambda edge: edge["edge"]),
        "engines": sorted(engines, key=lambda engine: engine["iid"]),
    }
    return versioned(DEPLOYMENT_STATUS, body)
