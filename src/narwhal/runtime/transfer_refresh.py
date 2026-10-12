from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from ..types import Role

if TYPE_CHECKING:
    from ..serving.router.routing import NarwhalRouter

log = logging.getLogger("narwhal.transfer_refresh")


class TransferRefresh:
    def __init__(self) -> None:
        self.tasks: set[asyncio.Task[None]] = set()
        # A busy decode engine held out of placement until its work drains and it refreshes.
        self.held: str | None = None


def stale_consumers(router: NarwhalRouter) -> dict[str, list[str]]:
    held = router.transfer_refresh.held
    live = router.scheduler.live_instances()
    if held is not None and held in router.monitor.instances:
        live.append(router.monitor.instances[held])
    producers = [inst.iid for inst in live if inst.role is Role.PREFILL]
    stale: dict[str, list[str]] = {}
    for inst in live:
        if inst.role is not Role.DECODE:
            continue
        if owed := [p for p in producers if router.stale_handoff(p, inst.iid)]:
            stale[inst.iid] = owed
    return stale


async def _refresh(router: NarwhalRouter, iid: str, producers: list[str]) -> None:
    state = router.transfer_refresh
    try:
        error = await router.refresh_transfer_state(iid)
    finally:
        if state.held == iid:
            state.held = None
    if error is None:
        log.info("refreshed %s's transfer state for %s", iid, ", ".join(producers))
    else:
        log.warning("transfer state refresh of %s failed: %s", iid, error)


def refresh_consumers(router: NarwhalRouter) -> None:
    """A producer that every live decode engine holds stale cannot hand off its KV, so the pass
    holds the least loaded of them out of new placements until its work drains.
    """
    state = router.transfer_refresh
    if router.role_switch is None or state.tasks:
        return
    stale = stale_consumers(router)
    instances = router.monitor.instances
    if state.held is not None and state.held not in stale:
        router.scheduler.availability.switching.discard(state.held)
        state.held = None
    idle = [iid for iid in stale if not instances[iid].prefill and not instances[iid].decode]
    if idle:
        iid = state.held if state.held in idle else idle[0]
        task = asyncio.create_task(_refresh(router, iid, stale[iid]))
        state.tasks.add(task)
        task.add_done_callback(state.tasks.discard)
        return
    if state.held is not None:
        return
    decodes = router.scheduler.role_pool(Role.DECODE, router.scheduler.live_instances())
    owed = {p for producers in stale.values() for p in producers}
    starved = any(not router.scheduler.has_fresh_consumer(p, decodes) for p in owed)
    if starved:
        held = min(stale, key=lambda iid: (len(instances[iid].decode), iid))
        router.scheduler.availability.switching.add(held)
        state.held = held
        log.info("holding %s out of placement to refresh its transfer state", held)


async def refresh_idle_consumers(router: NarwhalRouter) -> None:
    if router.role_switch is None:
        return
    instances = router.monitor.instances
    for iid, producers in stale_consumers(router).items():
        if not instances[iid].prefill and not instances[iid].decode:
            await _refresh(router, iid, producers)
