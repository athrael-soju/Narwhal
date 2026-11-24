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

from ..config.loading import read_engine_spec
from ..config.serialization import engine_row
from ..contracts import HANDOFF, validate_document, versioned
from ..profiling.membership import engine_set_problem
from ..profiling.store import ProfileStore
from ..types import Role

if TYPE_CHECKING:
    from ..serving.router.membership import Membership
    from ..serving.router.routing import NarwhalRouter


log = logging.getLogger("narwhal.state")

HANDOFF_COUNTERS = (
    "offered",
    "unsized_offered",
    "served",
    "slo_met",
    "expired",
    "invalid_requests",
    "failed",
    "refused",
    "rejected",
    "cancelled",
)


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
    # True when the handoff's membership replaced this router's engine set.
    membership_adopted: bool = False
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
        "engines": sorted(router.membership.ids),
        # The engine specs and generation a replacement process adopts.
        "membership": {
            "generation": router.membership.generation,
            "engines": [engine_row(spec) for spec in router.membership],
        },
        "roles": {iid: i.role.value for iid, i in router.monitor.instances.items()},
        "ejected": sorted(router.scheduler.ejected),
        "inference_sources": {
            iid: sorted(router.verifier.sources.get(iid, {""}))
            for iid in sorted(router.scheduler.inference_suspects)
        },
        "counters": {
            **{name: getattr(router, name) for name in HANDOFF_COUNTERS},
            "unserved": router.scheduler.unserved,
        },
        # Reason counts behind the failed, refused, rejected and expired counters.
        "outcome_reasons": {
            terminal: dict(counts) for terminal, counts in router.outcome_reasons.items()
        },
        "lifecycle": router.lifecycle.handoff(),
        # The newest risk event's age and counts, without arrival evidence.
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
    membership = doc.get("membership")
    if membership is not None:
        _validate_membership(membership)
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
        or not isinstance(peers, list)
        or not peers
        or any(not isinstance(peer, str) or (peer and peer not in engines) for peer in peers)
        for iid, peers in sources.items()
    ):
        raise ValueError("handoff inference_sources must name configured engines and peers")
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


def _validate_membership(membership: Any) -> None:
    # Keys a later release adds are ignored.
    if not isinstance(membership, dict) or not {"generation", "engines"} <= set(membership):
        raise ValueError("handoff membership must hold generation and engines")
    generation = membership["generation"]
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        raise ValueError("handoff membership generation must be a nonnegative integer")
    rows = membership["engines"]
    if not isinstance(rows, list):
        raise ValueError("handoff membership engines must be a list")
    ids = []
    for index, row in enumerate(rows):
        label = f"handoff membership engines[{index}]"
        ids.append(read_engine_spec(row, label, strict=False).iid)
    if len(set(ids)) != len(ids):
        raise ValueError("handoff membership names an engine twice")


def membership_of(doc: dict[str, Any]) -> Membership | None:
    """Return the snapshot a validated handoff carries, or None for one without membership."""
    from ..serving.router.membership import Membership

    saved = doc.get("membership")
    if saved is None:
        return None
    return Membership(
        saved["generation"],
        tuple(read_engine_spec(row, strict=False) for row in saved["engines"]),
    )


def _merged(router: NarwhalRouter, saved: Membership) -> Membership:
    """Return `saved` with this router's own spec for each engine it already knows.

    The saved generation and engine set win. An engine this router's fleet file names keeps the
    spec from that file, so its edits and host-specific endpoints hold; an engine that joined
    takes the saved spec.
    """
    from ..serving.router.membership import Membership

    current = router.membership
    specs = []
    for spec in saved:
        if spec.iid in current:
            own = current.spec(spec.iid)
            if own != spec:
                log.warning(
                    "handoff membership: %s keeps this router's engine entry, not the saved one",
                    spec.iid,
                )
            specs.append(own)
        else:
            specs.append(spec)
    return Membership(saved.generation, tuple(specs))


def _adoption_problem(router: NarwhalRouter, saved: Membership) -> tuple[str, ProfileStore | None]:
    """Return why `saved` cannot replace the router's snapshot, and the reloaded store."""
    current = router.membership
    if saved.generation < current.generation:
        return (
            f"handoff membership generation {saved.generation} precedes this router's "
            f"generation {current.generation}",
            None,
        )
    changed = {
        spec.iid
        for spec in (*current, *saved)
        if (spec.iid not in current or spec.iid not in saved)
        or current.spec(spec.iid) != saved.spec(spec.iid)
    }
    shared = sorted(
        iid
        for iid in changed
        if (iid in current and current.spec(iid).shared_device is not None)
        or (iid in saved and saved.spec(iid).shared_device is not None)
    )
    if shared:
        return f"handoff membership changes shared-device engines {', '.join(shared)}", None
    try:
        store = ProfileStore(router.cfg.profiles_path)
    except (OSError, ValueError) as exc:
        return f"profile store {router.cfg.profiles_path} is unreadable: {exc}", None
    problem = engine_set_problem(store, saved.ids, "saved", exact=False)
    if problem:
        return f"handoff membership lacks profiles: {problem}", None
    return "", store


def persist(router: NarwhalRouter) -> None:
    """Write the router's handoff now; log a failure, which the next monitoring pass retries."""
    try:
        write(router.cfg.state_path, snapshot(router))
    except OSError as exc:
        log.warning("handoff write to %s failed: %s", router.cfg.state_path, exc)


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


def _restore_reasons(router: NarwhalRouter, saved: Any) -> None:
    """Restore reason counts; counts a handoff leaves unexplained stay `unclassified`."""
    for terminal, counts in router.outcome_reasons.items():
        counts.clear()
        entries = saved.get(terminal) if isinstance(saved, dict) else None
        if isinstance(entries, dict):
            counts.update(
                {
                    str(reason): count
                    for reason, count in entries.items()
                    if isinstance(count, int) and not isinstance(count, bool) and count > 0
                }
            )
        missing = getattr(router, terminal) - sum(counts.values())
        if missing > 0:
            counts["unclassified"] += missing


def apply(router: NarwhalRouter, doc: dict[str, Any] | None) -> HandoffReport:
    """Restore saved router state and hold failed engines for recovery probes."""
    now_wall = time.time()
    if doc is None:
        return HandoffReport(applied=False, why="no handoff file")
    doc = validate(doc)
    declared = sorted(doc.get("engines") or [])
    saved = membership_of(doc)
    if saved is not None and saved.generation == 0:
        # Membership never changed: the fleet file's engine set and entries apply.
        saved = None
    if saved is not None:
        saved = _merged(router, saved)
    store: ProfileStore | None = None
    if saved is None:
        actual = sorted(router.membership.ids)
        if declared != actual:
            return HandoffReport(
                applied=False, why=f"handoff names {declared}, this fleet is {actual}"
            )
    else:
        # The saved membership replaces this router's engine set.
        actual = declared
        if sorted(saved.ids) != declared:
            return HandoffReport(
                applied=False, why=f"handoff membership differs from its engine list {declared}"
            )
        why, store = _adoption_problem(router, saved)
        if why:
            return HandoffReport(applied=False, why=why)
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
    adopted = saved is not None and store is not None
    if saved is not None and store is not None:
        router.adopt_membership(saved)
        router.profiles.adopt(store)
    gap_s = max(0.0, now_wall - float(doc.get("at", now_wall)))
    roles = doc.get("roles") or {}
    applied = 0
    pinned: frozenset[str] = router.scheduler.pinned
    # Dwell and resident tracking start fresh.
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
    ejected = [iid for iid in doc.get("ejected", []) if iid in router.membership]
    for iid in ejected:
        # A far-past ejection time makes the readmission probe due at once.
        router.scheduler.ejected[iid] = now - 1e9
    for iid, peers in doc.get("inference_sources", {}).items():
        router.scheduler.inference_suspects.add(iid)
        router.verifier.sources[iid] = set(peers)
        # A suspect resolves through a readmission probe while another engine covers its role.
        if iid not in router.scheduler.ejected and router.scheduler.role_covered_without(iid):
            router.scheduler.ejected[iid] = now - 1e9
            ejected.append(iid)
    # apply() bypasses the scheduler paths that normally update floor state.
    router.scheduler.refresh_floor_state()

    counters = doc.get("counters") or {}
    for name in HANDOFF_COUNTERS:
        setattr(router, name, int(counters.get(name, 0)))
    router.scheduler.unserved = int(counters.get("unserved", 0))
    _restore_reasons(router, doc.get("outcome_reasons"))
    router.lifecycle.restore(doc.get("lifecycle"))
    controller = router.controller
    risk = doc.get("demand_risk")
    if controller is not None and isinstance(risk, dict):
        # The carried event re-arms with its age re-anchored on this process's clock.
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
        membership_adopted=adopted,
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
