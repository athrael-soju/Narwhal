"""Persist router state for restart and warm-standby takeover."""

from __future__ import annotations

import json
import logging
import math
import os
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..contracts import HANDOFF, validate_document, versioned
from ..types import Role

if TYPE_CHECKING:
    from ..serving.router import NarwhalRouter


log = logging.getLogger("narwhal.state")


@dataclass
class HandoffReport:
    """Result of applying a saved handoff."""

    applied: bool
    why: str = ""
    gap_s: float | None = None
    run: str = ""
    roles_applied: int = 0
    ejected: list[str] = field(default_factory=list)
    served: int = 0
    failed: int = 0
    unserved: int = 0
    epoch: int = 0
    holder: str = ""


def snapshot(router: NarwhalRouter) -> dict[str, Any]:
    """Capture the router state required by a replacement process."""
    controller = router.controller
    out = {
        "at": time.time(),
        "run": router.journal.run,
        "model": router.cfg.model,
        "epoch": int(router.lease_epoch),
        "holder": str(router.lease_holder),
        "engines": sorted(router.monitor.instances),
        "roles": {iid: i.role.value for iid, i in router.monitor.instances.items()},
        "ejected": sorted(set(router.scheduler.ejected) | router.scheduler.inference_suspects),
        "inference_sources": {
            iid: sorted(router._inference_sources.get(iid, {""}))
            for iid in sorted(router.scheduler.inference_suspects)
        },
        "counters": {
            "served": router.served,
            "failed": router.failed,
            "unserved": router.scheduler.unserved,
            "refused": router.refused,
            "rejected": router.rejected,
            "cancelled": router.cancelled,
        },
        "lifecycle": router.lifecycle.handoff(),
        # Save the newest risk event's age and counts. The receiver collects
        # fresh arrival evidence for its consolidation window.
        "demand_risk": (controller.safety.risk_handoff() if controller is not None else None),
    }
    return versioned(HANDOFF, out)


def validate(doc: Any) -> dict[str, Any]:
    """Validate the handoff schema and saved state."""
    validate_document(doc, HANDOFF)
    epoch = doc.get("epoch", 0)
    holder = doc.get("holder", "")
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
        raise ValueError("handoff epoch must be a nonnegative integer")
    if not isinstance(holder, str):
        raise ValueError("handoff holder must be a string")
    lifecycle = doc.get("lifecycle")
    if not isinstance(lifecycle, dict):
        raise ValueError("handoff requires lifecycle state")
    if not isinstance(lifecycle.get("records"), list):
        raise ValueError("handoff lifecycle records must be a list")
    if any(
        not isinstance(row, dict) or not isinstance(row.get("iid"), str)
        for row in lifecycle["records"]
    ):
        raise ValueError("handoff lifecycle records must name engines")
    if not isinstance(lifecycle.get("wave_id", ""), str):
        raise ValueError("handoff lifecycle wave_id must be a string")
    if lifecycle.get("engine_restart_policy") not in ("individual", "whole_wave"):
        raise ValueError("handoff lifecycle engine_restart_policy is invalid")
    starts = lifecycle.get("process_starts")
    if not isinstance(starts, dict) or any(
        iid not in doc.get("engines", [])
        or isinstance(start, bool)
        or not isinstance(start, int | float)
        or not math.isfinite(start)
        or start <= 0
        for iid, start in starts.items()
    ):
        raise ValueError("handoff process_starts must map configured engines to positive times")
    sources = doc.get("inference_sources", {})
    engines = doc.get("engines", [])
    if not isinstance(sources, dict) or any(
        iid not in engines
        or iid not in doc.get("ejected", [])
        or not isinstance(peers, list)
        or not peers
        or any(not isinstance(peer, str) or (peer and peer not in engines) for peer in peers)
        for iid, peers in sources.items()
    ):
        raise ValueError("handoff inference_sources must name held engines and configured peers")
    risk = doc.get("demand_risk")
    if risk is not None:
        if not isinstance(risk, dict):
            raise ValueError("handoff demand_risk must be an object or null")
        kind = risk.get("kind")
        if not isinstance(kind, str):
            raise ValueError("handoff demand_risk.kind must be a string")
        age_s = risk.get("age_s", 0.0)
        if (
            isinstance(age_s, bool)
            or not isinstance(age_s, (int, float))
            or not math.isfinite(age_s)
            or age_s < 0
        ):
            raise ValueError("handoff demand_risk.age_s must be a nonnegative number")
        events = risk.get("events")
        if not isinstance(events, dict):
            raise ValueError("handoff demand_risk.events must be an object")
        for name, value in events.items():
            if not isinstance(name, str):
                raise ValueError("handoff demand_risk.events keys must be strings")
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("handoff demand_risk.events values must be nonnegative integers")
    return dict(doc)


def write(path: Path, doc: dict[str, Any]) -> None:
    """Validate the handoff, fsync a unique temporary file and atomically
    replace the destination.
    """
    validate(doc)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w") as handle:
            handle.write(json.dumps(doc) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        with suppress(FileNotFoundError):
            tmp.unlink()


def load(path: Path) -> dict[str, Any] | None:
    """Load a compatible handoff, or return None for a missing or torn file."""
    try:
        doc = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return validate(doc)


def apply(router: NarwhalRouter, doc: dict[str, Any] | None) -> HandoffReport:
    """Restore saved router state and hold failed engines for recovery probes."""
    now_wall = time.time()
    if doc is None:
        return HandoffReport(applied=False, why="no handoff file")
    doc = validate(doc)
    declared = sorted(doc.get("engines") or [])
    actual = sorted(router.monitor.instances)
    if declared != actual:
        return HandoffReport(applied=False, why=f"handoff names {declared}, this fleet is {actual}")
    lifecycle = doc.get("lifecycle") or {}
    if lifecycle["engine_restart_policy"] != router.cfg.engine_restart_policy:
        return HandoffReport(applied=False, why="handoff engine restart policy mismatch")
    if router.cfg.engine_contract is not None:
        held = {row["iid"] for row in lifecycle.get("records", []) if row.get("state") != "active"}
        eligible = set(actual) - set(doc.get("ejected", [])) - held
        if not eligible.issubset(lifecycle.get("process_starts", {})):
            return HandoffReport(
                applied=False, why="handoff lacks an available engine's process identity"
            )
    gap_s = max(0.0, now_wall - float(doc.get("at", now_wall)))
    roles = doc.get("roles") or {}
    applied = 0
    pinned: frozenset[str] = router.scheduler.pinned
    # Restore assignments directly; dwell and resident tracking start fresh.
    for iid, name in roles.items():
        if iid in pinned:
            # Fleet configuration owns pinned roles across restarts.
            continue
        try:
            router.monitor.instances[iid].role = Role(name)
            applied += 1
        except (KeyError, ValueError):
            continue

    now = router._clock()
    ejected = [iid for iid in doc.get("ejected", []) if iid in router.monitor.instances]
    for iid in ejected:
        # Force an immediate readmission probe after restart.
        router.scheduler.ejected[iid] = now - 1e9
    for iid, peers in doc.get("inference_sources", {}).items():
        router.scheduler.inference_suspects.add(iid)
        router._inference_sources[iid] = set(peers)
    # apply() bypasses the scheduler paths that normally update floor state.
    router.scheduler.refresh_floor_state()

    counters = doc.get("counters") or {}
    router.served = int(counters.get("served", 0))
    router.failed = int(counters.get("failed", 0))
    router.scheduler.unserved = int(counters.get("unserved", 0))
    # Admission counters are cumulative across router restarts.
    router.refused = int(counters.get("refused", 0))
    router.rejected = int(counters.get("rejected", 0))
    router.cancelled = int(counters.get("cancelled", 0))
    router.lifecycle.restore(doc.get("lifecycle"))
    controller = router.controller
    risk = doc.get("demand_risk")
    if controller is not None and isinstance(risk, dict):
        # An absent or null block restores no armed event; a carried one
        # re-arms with its elapsed age re-anchored on this process's clock.
        controller.safety.restore_risk(
            kind=str(risk["kind"]),
            age_s=float(risk.get("age_s", 0.0)),
            events={str(name): int(count) for name, count in risk.get("events", {}).items()},
        )

    report = HandoffReport(
        applied=True,
        gap_s=gap_s,
        run=str(doc.get("run", "")),
        roles_applied=applied,
        ejected=ejected,
        served=router.served,
        failed=router.failed,
        unserved=router.scheduler.unserved,
        epoch=int(doc.get("epoch", 0)),
        holder=str(doc.get("holder", "")),
    )
    log.info(
        "handoff applied: gap %.2fs from run %s; %d roles, %d ejected; "
        "counters served=%d failed=%d unserved=%d",
        report.gap_s,
        report.run,
        report.roles_applied,
        len(report.ejected),
        report.served,
        report.failed,
        report.unserved,
    )
    return report
