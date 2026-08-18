"""The control-plane handoff: what a restart must remember, in one file.

The design question is what a dead router owes its replacement. Request state
lives on the engines (stateless, §5.2), so nothing in flight is owed; what
the replacement cannot rebuild cheaply is the *actuated* picture: which pool
assignments the controllers moved beyond the fleet JSON's opening split,
which engines the breaker holds and owes a probe, and the counters the run
speaks over. Loads rebuild from traffic, profiles sit on disk, admissions
re-tally as probes answer — this file hands the rest over.

It is rewritten atomically every monitoring pass (write a sibling name, then
rename(2)), so a kill mid-write never hands a torn document down, and the
lifespan writes one final snapshot at shutdown, which is the warm-standby
handoff's freshest possible source. `apply` is the resume path: instances are
relabelled in place, each ejected engine keeps its status but owes its probe
immediately, offline relaunch windows keep their remaining seconds, and the
counters continue — the gap the restart paid is measured from the snapshot's
wall-clock stamp and logged, the run's MTTR figure, printed by the
system rather than estimated around it.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .types import Role

log = logging.getLogger("narwhal.state")

VERSION = 1


@dataclass
class HandoffReport:
    """What one `apply` rebuilt — so the resume line can say it, and a test can hold it."""

    applied: bool
    why: str = ""
    gap_s: float | None = None
    run: str = ""
    roles_applied: int = 0
    ejected: list[str] = field(default_factory=list)
    offline: dict[str, float] = field(default_factory=dict)
    served: int = 0
    failed: int = 0
    unserved: int = 0


def snapshot(router: Any, *, wall: float | None = None) -> dict[str, Any]:
    """The whole handoff, small enough to rewrite every monitoring pass."""
    remaining = {
        iid: max(0.0, at - router._clock())
        for iid, at in router.scheduler.offline_until.items()
        if at > router._clock()
    }
    return {
        "version": VERSION,
        "at": wall if wall is not None else time.time(),
        "run": router.journal.run,
        "model": router.cfg.model,
        "engines": sorted(router.monitor.instances),
        "roles": {iid: i.role.value for iid, i in router.monitor.instances.items()},
        "ejected": sorted(router.scheduler.ejected),
        "offline_remaining": remaining,
        "counters": {
            "served": router.served,
            "failed": router.failed,
            "unserved": router.scheduler.unserved,
            "refused": router.refused,
            "rejected": router.rejected,
        },
    }


def write(path: Path, doc: dict[str, Any]) -> None:
    """Atomically replace the handoff: rename(2) is the whole crash protocol."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc) + "\n")
    os.replace(tmp, path)


def load(path: Path) -> dict[str, Any] | None:
    """A torn or absent handoff is not an error: the router just opens cold."""
    try:
        doc = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        # OSError covers absent, unreadable and directory-shaped paths; a
        # crash here would kill the exact --resume startup this file exists
        # to protect, so every unreadable shape opens cold instead.
        return None
    if not isinstance(doc, dict) or doc.get("version") != VERSION:
        return None
    return doc


def apply(router: Any, doc: dict[str, Any] | None, *, wall: float | None = None) -> HandoffReport:
    """Rebuild the late router's actuated picture onto this one.

    Refuses when the handoff names a different fleet — a different fleet's
    pools would misprice everything, and refusing loudly is the honest gap.
    An all-ejected handoff would leave the replacement nowhere to send a
    request and nothing to probe from: it lands as no ejections instead, with
    a warning.
    """
    now_wall = wall if wall is not None else time.time()
    if doc is None:
        return HandoffReport(applied=False, why="no handoff file")
    declared = sorted(doc.get("engines") or [])
    actual = sorted(router.monitor.instances)
    if declared != actual:
        return HandoffReport(applied=False, why=f"handoff names {declared}, this fleet is {actual}")
    gap_s = max(0.0, now_wall - float(doc.get("at", now_wall)))
    roles = doc.get("roles") or {}
    applied = 0
    pinned: frozenset[str] = getattr(router.scheduler, "pinned", frozenset())
    for iid, name in roles.items():
        if iid in pinned:
            # A pinned engine's role is configuration, not actuated state:
            # the handoff never overrides it.
            continue
        try:
            router.monitor.instances[iid].role = Role(name)
            applied += 1
        except (KeyError, ValueError):
            continue

    now = router._clock()
    ejected = [iid for iid in doc.get("ejected", []) if iid in router.monitor.instances]
    if len(ejected) >= len(router.monitor.instances):
        log.warning(
            "handoff: every engine listed as ejected; opening with none, "
            "the breaker earns each from fresh failures"
        )
        ejected = []
    for iid in ejected:
        # Due at the very next readmission pass: dead engines get exactly one
        # probe of doubt across the restart, live ones readmit at once.
        router.scheduler.ejected[iid] = now - 1e9
    for iid, rem in (doc.get("offline_remaining") or {}).items():
        if iid in router.monitor.instances and rem > 0.0:
            router.scheduler.offline_until[iid] = now + float(rem)

    counters = doc.get("counters") or {}
    router.served = int(counters.get("served", 0))
    router.failed = int(counters.get("failed", 0))
    router.scheduler.unserved = int(counters.get("unserved", 0))
    # The door's books continue too: a restart must not zero the refusal
    # story the run has been telling (the refusal KPI rides on it).
    router.refused = int(counters.get("refused", 0))
    router.rejected = int(counters.get("rejected", 0))

    report = HandoffReport(
        applied=True,
        gap_s=gap_s,
        run=str(doc.get("run", "")),
        roles_applied=applied,
        ejected=ejected,
        offline={
            iid: float(r) for iid, r in (doc.get("offline_remaining") or {}).items() if float(r) > 0
        },
        served=router.served,
        failed=router.failed,
        unserved=router.scheduler.unserved,
    )
    log.info(
        "handoff applied: gap %.2fs from run %s; %d roles, %d ejected, %d offline windows; "
        "counters served=%d failed=%d unserved=%d",
        report.gap_s,
        report.run,
        report.roles_applied,
        len(report.ejected),
        len(report.offline),
        report.served,
        report.failed,
        report.unserved,
    )
    return report
