"""Lease-fenced warm-standby polling and takeover."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING, Any

import httpx

from ..contracts import ContractVersionError
from . import state as handoff_state
from .lease import FileLease, LeaseError

if TYPE_CHECKING:
    from ..serving.router import NarwhalRouter


log = logging.getLogger("narwhal.standby")

PROBE_INTERVAL_S = 0.25
TAKEOVER_AFTER = 4
MAX_HANDOFF_AGE_S = 30.0


def controls_fleet(router: NarwhalRouter) -> bool:
    """Return whether this process owns the active router lease."""
    lease: FileLease | None = router.lease
    return not router.standby and (lease is None or lease.valid())


def control_ready(router: NarwhalRouter) -> bool:
    """Keep backend availability separate from router control failures."""
    return (
        controls_fleet(router)
        # Monitoring failure counts as a missed standby probe even while the
        # primary still owns its lease. Backend loss keeps handoffs flowing.
        and not router.monitoring_degraded
    )


def ready(router: NarwhalRouter) -> bool:
    """Return whether this process may admit new traffic."""
    return (
        control_ready(router)
        and router.lifecycle.identities_ready
        and not router.lifecycle_blocked
        and bool(router.scheduler.live_instances())
    )


async def lease_renew_loop(router: NarwhalRouter, interval_s: float) -> None:
    """Renew owned epochs and fence the router on any renewal failure."""
    lease = router.lease
    if lease is None:
        raise ValueError("lease renewal requires a configured lease")
    while True:
        await asyncio.sleep(interval_s)
        if not lease.owned:
            continue
        if lease.renew():
            router.lease_epoch = lease.epoch
            router.lease_holder = lease.holder
            continue
        router.standby = True
        router.failover_blocked = "lease renewal failed"
        log.error("router fenced: lease renewal failed for epoch %d", lease.epoch)
        return


def _fresh(doc: dict[str, Any] | None, max_age_s: float) -> bool:
    if doc is None:
        return False
    try:
        age = max(0.0, time.time() - float(doc["at"]))
    except (KeyError, TypeError, ValueError):
        return False
    return age <= max_age_s


async def standby_loop(
    router: NarwhalRouter,
    primary: str,
    lease: FileLease,
    *,
    probe_interval_s: float = PROBE_INTERVAL_S,
    takeover_after: int = TAKEOVER_AFTER,
    max_handoff_age_s: float = MAX_HANDOFF_AGE_S,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Shadow an active primary and claim its expired lease before takeover.

    Loss of the primary HTTP path is insufficient while its shared lease is
    valid. A missing, stale, incompatible, or wrong-epoch handoff keeps this
    process non-ready.
    """
    doc = handoff_state.load(router.cfg.state_path)
    misses = 0
    last_good = time.monotonic()
    async with httpx.AsyncClient(timeout=probe_interval_s * 0.8, transport=transport) as client:
        while True:
            try:
                readiness = await client.get(f"{primary}/ready")
                if readiness.status_code == 503:
                    status = readiness.json()
                    if not isinstance(status, dict) or status.get("control_ready") is not True:
                        readiness.raise_for_status()
                else:
                    readiness.raise_for_status()
                response = await client.get(f"{primary}/narwhal/handoff")
                response.raise_for_status()
                fresh = handoff_state.validate(response.json())
                shared = lease.read()
                if (
                    shared is None
                    or shared.holder != fresh.get("holder")
                    or shared.epoch != fresh.get("epoch")
                    or shared.expires_at <= time.time()
                ):
                    raise LeaseError("primary handoff does not own the shared lease")
            except ContractVersionError:
                raise
            except (httpx.HTTPError, LeaseError, ValueError):
                misses += 1
            else:
                doc, misses = fresh, 0
                last_good = time.monotonic()
                with contextlib.suppress(OSError):
                    handoff_state.write(router.cfg.state_path, doc)

            if misses >= takeover_after:
                if not _fresh(doc, max_handoff_age_s):
                    router.failover_blocked = "no fresh handoff"
                else:
                    try:
                        claimed = lease.claim()
                    except LeaseError as exc:
                        router.failover_blocked = f"lease unavailable: {exc}"
                    else:
                        if claimed:
                            if doc is None:
                                router.failover_blocked = "no handoff after lease claim"
                                lease.release()
                                return
                            previous_epoch = int(doc.get("epoch", 0))
                            if previous_epoch != lease.epoch - 1:
                                router.failover_blocked = (
                                    f"handoff epoch {previous_epoch} cannot precede "
                                    f"claimed epoch {lease.epoch}"
                                )
                                lease.release()
                                return
                            try:
                                report = handoff_state.apply(router, doc)
                                if not report.applied:
                                    router.failover_blocked = report.why
                                    return
                                if not lease.valid():
                                    router.failover_blocked = "lease expired while applying handoff"
                                    return
                                router.lease_epoch = lease.epoch
                                router.lease_holder = lease.holder
                                from .lifecycle import check_process_identities

                                router.lifecycle.identities_ready = (
                                    router.cfg.engine_contract is None
                                )
                                router.controller.clear_prefill_risk()
                                router.control_wakeup.clear()
                                router.standby = False
                                try:
                                    await check_process_identities(router)
                                except BaseException:
                                    router.standby = True
                                    router.failover_blocked = "identity validation interrupted"
                                    raise
                                if not lease.valid():
                                    router.standby = True
                                    router.failover_blocked = (
                                        "lease expired during identity validation"
                                    )
                                    return
                                router.failover_blocked = ""
                                router.takeover_gap_s = time.monotonic() - last_good
                                log.warning(
                                    "TAKEOVER: claimed epoch %d after %d failed probes; "
                                    "applied run %s with %d roles; %.2fs since last good answer",
                                    lease.epoch,
                                    misses,
                                    report.run,
                                    report.roles_applied,
                                    router.takeover_gap_s,
                                )
                                return
                            finally:
                                if router.standby:
                                    lease.release()
            await asyncio.sleep(probe_interval_s)
