"""A warm standby, so the control plane survives its node.

Direction 1 made the restart cheap: everything the router must remember is
one small handoff document, rewritten every monitoring pass. This module
makes it warm. A second router process starts with `--standby-of` pointing
at the primary, polls that document over HTTP, and refuses traffic while
the primary answers. When the primary goes silent for `takeover_after`
consecutive probes, the standby applies the freshest document it holds and
opens its door. Detection plus takeover is a handful of probe intervals -
sub-second at the defaults - and the applied document is at most one
monitoring pass old, so the actuated picture (roles, the breaker's holds,
the counters the run speaks over) continues rather than restarts.

What this module does not solve is the address: clients that point at the
dead primary's host need a VIP, a DNS flip, or a TCP balancer in front of
the pair. That is deployment furniture, deliberately outside the router.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

import httpx

from . import state as handoff_state

log = logging.getLogger("narwhal.standby")

PROBE_INTERVAL_S = 0.25
TAKEOVER_AFTER = 4


async def standby_loop(
    router: Any,
    primary: str,
    *,
    probe_interval_s: float = PROBE_INTERVAL_S,
    takeover_after: int = TAKEOVER_AFTER,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Poll the primary's handoff; take over when it goes silent.

    The polled document is also written to this process's own state path,
    so a standby that crashes and returns still holds the last picture it
    saw rather than nothing.
    """
    doc: dict[str, Any] | None = None
    misses = 0
    last_good = time.monotonic()
    async with httpx.AsyncClient(timeout=probe_interval_s * 0.8, transport=transport) as c:
        while True:
            try:
                r = await c.get(f"{primary}/arrow/handoff")
                r.raise_for_status()
                fresh = r.json()
            except (httpx.HTTPError, ValueError):
                misses += 1
                if misses >= takeover_after:
                    break
            else:
                doc, misses = fresh, 0
                last_good = time.monotonic()
                # A full disk must not kill the watch itself.
                with contextlib.suppress(OSError):
                    handoff_state.write(router.cfg.state_path, doc)
            await asyncio.sleep(probe_interval_s)

    gap_s = time.monotonic() - last_good
    report = handoff_state.apply(router, doc)
    router.standby = False
    router.takeover_gap_s = gap_s
    log.warning(
        "TAKEOVER: primary %s silent for %d probes; applied handoff (%s), "
        "%.2fs since its last good answer; this router is now active",
        primary,
        takeover_after,
        f"run {report.run}, {report.roles_applied} roles" if report.applied else report.why,
        gap_s,
    )
