"""Remove an engine from the fleet after it drains."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import httpx

from ..standby import controls_fleet
from .identity import capture_process_identities
from .records import DrainRecord, LifecycleError

if TYPE_CHECKING:
    from ...serving.router.routing import NarwhalRouter


# Held states a leave takes over: the engine is out of placement and stays drained.
TAKEN_OVER = frozenset({"drained", "deadline_exceeded", "blocked"})


def _held_alone(router: NarwhalRouter, iid: str) -> bool:
    """Return True when `iid` is the only held engine, outside a wave, in a state a leave
    takes over, such as a failed readmission."""
    records = router.lifecycle.records
    record = records.get(iid)
    others = [i for i, rec in records.items() if i != iid and rec.state != "active"]
    return record is not None and not record.wave_id and record.state in TAKEN_OVER and not others


def leave_refusal(router: NarwhalRouter, iid: str) -> str:
    """Return why `iid` cannot leave the fleet now, or ""."""
    cfg = router.cfg
    membership = router.membership
    if cfg.engine_restart_policy == "whole_wave":
        return "engine_restart_policy whole_wave changes engines only as one managed wave"
    if cfg.engine_contract is None or cfg.contract_missing():
        return "removing an engine requires a complete engine_contract"
    if iid not in membership:
        return f"unknown engine: {iid}"
    if membership.spec(iid).shared_device is not None:
        return "an engine in a shared-device group cannot leave"
    held = sorted(i for i, rec in router.lifecycle.records.items() if rec.state != "active")
    if held and not _held_alone(router, iid):
        return f"lifecycle action already active for {', '.join(held)}"
    if not router.scheduler.live_instances(exclude={iid}):
        return "the last schedulable engine cannot leave"
    floor = router.scheduler.min_prefill + router.scheduler.min_decode
    if len(membership) - 1 < floor:
        return (
            f"leaving would keep {len(membership) - 1} engine(s), fewer than "
            f"min_prefill + min_decode = {floor}"
        )
    return ""


async def begin_leave(
    router: NarwhalRouter,
    iid: str,
    *,
    deadline_s: float,
    transport: httpx.AsyncBaseTransport | None = None,
) -> DrainRecord:
    """Start the drain that removes `iid` once it completes; raise LifecycleError on refusal.

    The drain captures the engine's process identity as a planned drain does. A retry of a
    leave whose identity capture failed captures it again. The caller holds
    `router.lifecycle.lock`.
    """
    manager = router.lifecycle
    retry = (record := manager.records.get(iid)) is not None and record.leaving
    try:
        if not retry and (refusal := leave_refusal(router, iid)):
            raise LifecycleError(refusal)
        if retry and record is not None and record.state == "draining":
            # The leave is already draining the engine.
            pass
        elif _held_alone(router, iid):
            # The engine is already out of placement; a failed readmission or identity capture
            # leaves it blocked. It drains again under the leave's deadline, so it reports
            # drained only once its resident work is gone and its identity is recorded.
            record = manager.records[iid]
            if record.state == "blocked":
                record.state = "draining"
                record.error = ""
                record.deadline_at = time.time() + deadline_s
        else:
            manager.begin([iid], wave=False, deadline_s=deadline_s)
    except LifecycleError:
        manager.membership_counts.leaves["refused"] += 1
        raise
    record = manager.records[iid]
    if not record.leaving:
        record.leaving = True
        manager._emit("leave_started", iid=iid, generation=router.membership.generation)
    if record.old_process_start is None:
        starts, failures = await capture_process_identities(
            router.cfg, [iid], specs=router.membership.by_id(), transport=transport
        )
        manager.bind_drain_identities([iid], starts, failures)
    manager.refresh()
    # A router fenced during the identity capture leaves the removal to the next owner.
    if controls_fleet(router):
        complete_leaves(router)
    return record


def complete_leaves(router: NarwhalRouter) -> list[str]:
    """Remove each leaving engine whose drain is complete and that no handoff still reads.

    An engine leaves the snapshot once it is drained with no resident request and no decode
    leg on another engine still pulls KV from it. Returns the engines removed.
    """
    manager = router.lifecycle
    manager.refresh()
    monitor = router.monitor
    done = [
        iid
        for iid, record in sorted(manager.records.items())
        if record.leaving
        and record.state == "drained"
        and iid in router.membership
        and not monitor.instances[iid].prefill
        and not monitor.instances[iid].decode
        and not monitor.pending_handoffs(iid)
    ]
    for iid in done:
        process_start = manager.records[iid].old_process_start
        membership = router.membership.left(iid)
        router.apply_membership(membership)
        manager.membership_counts.leaves["left"] += 1
        manager._emit(
            "left",
            iid=iid,
            generation=membership.generation,
            process_start=process_start,
        )
    return done
