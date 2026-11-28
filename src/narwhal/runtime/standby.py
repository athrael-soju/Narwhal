"""Lease-fenced warm-standby polling and takeover."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING, Any

import httpx

from ..contracts import ContractVersionError
from ..profiling.calibration import verify_calibration
from ..profiling.membership import engine_set_problem
from . import state as handoff_state
from .lease import LeaseError, RouterLease

if TYPE_CHECKING:
    from ..serving.router.routing import NarwhalRouter


log = logging.getLogger("narwhal.standby")

PROBE_INTERVAL_S = 0.25
TAKEOVER_AFTER = 4
MAX_HANDOFF_AGE_S = 30.0


def controls_fleet(router: NarwhalRouter) -> bool:
    """Return whether this process owns the active router lease."""
    lease: RouterLease | None = router.lease
    return not router.standby and (lease is None or lease.valid())


def control_ready(router: NarwhalRouter) -> bool:
    """Return whether router control is ready, independent of backend availability."""
    return (
        controls_fleet(router)
        # Degraded monitoring counts as a missed standby probe while the primary holds its lease.
        and not router.monitoring_degraded
    )


def lease_role(router: NarwhalRouter) -> str:
    """Return `active`, `standby` or `fenced`, the lease role `/health` and `/metrics` report.

    A router is fenced while a failover block holds it or its own lease has lapsed.
    """
    lease: RouterLease | None = router.lease
    lapsed = not router.standby and lease is not None and not lease.valid()
    if router.failover_blocked or lapsed:
        return "fenced"
    return "standby" if router.standby else "active"


def refusals_retried(router: NarwhalRouter) -> bool:
    """Return whether the load balancer can retry this router's refusals on a live lease holder.

    Only a standby whose last probe found its primary controlling the fleet knows of one. Every
    other router counts a request it refuses as a client outcome, because no other router may
    take the retry.
    """
    return router.standby and router.primary_seen


def ready(router: NarwhalRouter) -> bool:
    """Return whether this process may admit new traffic."""
    return (
        control_ready(router)
        and router.lifecycle.identities_ready
        and not router.lifecycle_blocked
        and bool(router.scheduler.live_instances())
    )


async def lease_renew_loop(router: NarwhalRouter, interval_s: float) -> None:
    """Renew owned epochs and fence the router on any renewal failure.

    A renewal runs in a worker thread, so a slow lease store never stalls serving. The router's
    fence does not wait for it: `valid()` lapses on the lease's own deadline.
    """
    lease = router.lease
    if lease is None:
        raise ValueError("lease renewal requires a configured lease")
    while True:
        await asyncio.sleep(interval_s)
        if not lease.owned:
            continue
        if await asyncio.to_thread(lease.renew):
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


def _cold(lease: RouterLease, max_age_s: float) -> bool:
    """Whether the pair starts cold: the lease is absent, or it expired longer ago than a
    handoff stays fresh, so no router of the pair can hold state worth taking over.

    A standby applies this while its own handoff is stale, so a pair that stopped together,
    both as standbys, elects a primary. Only a lease whose `cold_start` is true takes it, a
    Kubernetes Lease, whose claims are conditional updates: its expiry is this router's own
    estimate, from when it first saw the Lease unchanged, so a holder still renewing never
    reads as cold. A lease file never does; its first start applies task 10's rule.
    """
    if not getattr(lease, "cold_start", False):
        return False
    record = lease.read()
    return record is None or time.time() - record.expires_at > max_age_s


async def _takeover_problem(router: NarwhalRouter, report: handoff_state.HandoffReport) -> str:
    """Return why the router cannot serve the engine set the handoff left it, or "".

    The startup checks covered `router.checked_engines`. When the takeover serves another set, or
    the startup profile check allowed extra profiles, the calibration and profile-set checks run
    again over the served set. Ejected and draining engines are not read; recovery probes them.
    """
    served = tuple(router.membership.ids)
    if served == router.checked_engines and router.checked_exact:
        return ""
    exact = not report.membership_adopted
    if problem := engine_set_problem(router.profiles, served, "served", exact=exact):
        return problem
    if served == router.checked_engines:
        # The startup calibration check covered this set.
        router.checked_exact = True
        return ""
    scheduler = router.scheduler
    live = {iid for iid in served if iid not in scheduler.ejected and iid not in scheduler.draining}
    try:
        calibration = await verify_calibration(
            router.cfg,
            engines=router.membership.specs,
            live=live,
            transport=router.lifecycle_transport,
        )
    except Exception as exc:
        return f"first-token calibration check failed: {type(exc).__name__}: {exc}"
    if calibration.status == "rejected":
        return "; ".join(calibration.problems)
    router.first_token_calibration = calibration
    router.checked_engines = served
    router.checked_exact = True
    return ""


async def standby_loop(
    router: NarwhalRouter,
    primary: str,
    lease: RouterLease,
    *,
    probe_interval_s: float = PROBE_INTERVAL_S,
    takeover_after: int = TAKEOVER_AFTER,
    max_handoff_age_s: float = MAX_HANDOFF_AGE_S,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Shadow an active primary and claim its expired lease before takeover.

    A valid primary lease blocks takeover even when the primary HTTP path is lost. Once the loop
    ends, for any reason, no probe vouches for the primary, so the router counts its refusals as
    client outcomes again.
    """
    try:
        await _shadow(
            router,
            primary,
            lease,
            probe_interval_s=probe_interval_s,
            takeover_after=takeover_after,
            max_handoff_age_s=max_handoff_age_s,
            transport=transport,
        )
    finally:
        router.primary_seen = False


async def _shadow(
    router: NarwhalRouter,
    primary: str,
    lease: RouterLease,
    *,
    probe_interval_s: float,
    takeover_after: int,
    max_handoff_age_s: float,
    transport: httpx.AsyncBaseTransport | None,
) -> None:
    """Probe the primary and take over once it misses `takeover_after` probes in a row."""
    doc = handoff_state.load(router.cfg.state_path)
    misses = 0
    # The block a failed takeover attempt set while the loop keeps shadowing, or "".
    transient = ""
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
                shared = await asyncio.to_thread(lease.read)
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
                router.primary_seen = False
            else:
                doc, misses = fresh, 0
                router.primary_seen = True
                last_good = time.monotonic()
                if router.failover_blocked == transient:
                    # Shadowing resumed, so the standby can take over again.
                    router.failover_blocked = transient = ""
                with contextlib.suppress(OSError):
                    handoff_state.write(router.cfg.state_path, doc)

            if misses >= takeover_after:
                fresh_handoff = _fresh(doc, max_handoff_age_s)
                try:
                    cold = not fresh_handoff and await asyncio.to_thread(
                        _cold, lease, max_handoff_age_s
                    )
                    if not (fresh_handoff or cold):
                        router.failover_blocked = transient = "no fresh handoff"
                        claimed = False
                    else:
                        claimed = await asyncio.to_thread(lease.claim)
                except LeaseError as exc:
                    router.failover_blocked = transient = f"lease unavailable: {exc}"
                else:
                    if claimed:
                        await _take_control(
                            router, lease, None if cold else doc, misses=misses, last_good=last_good
                        )
                        return
            await asyncio.sleep(probe_interval_s)


async def _take_control(
    router: NarwhalRouter,
    lease: RouterLease,
    doc: dict[str, Any] | None,
    *,
    misses: int,
    last_good: float,
) -> None:
    """Serve on the lease this standby just claimed, from the handoff `doc`, or, on a cold
    start, from the router's own configuration. Any check that fails leaves it a blocked
    standby and releases the lease."""
    try:
        report = handoff_state.HandoffReport(applied=False, why="cold start")
        if doc is None and not getattr(lease, "cold_start", False):
            # Only a conditional claim may serve without the previous epoch's handoff.
            router.failover_blocked = "no handoff after lease claim"
            return
        if doc is not None:
            previous_epoch = int(doc.get("epoch", 0))
            if previous_epoch != lease.epoch - 1:
                router.failover_blocked = (
                    f"handoff epoch {previous_epoch} cannot precede claimed epoch {lease.epoch}"
                )
                return
        if delay := lease.serving_delay():
            # The claim may hide a holder this router never saw.
            log.warning(
                "claimed epoch %d; waiting %.1fs for any unseen holder's lease to lapse",
                lease.epoch,
                delay,
            )
            await asyncio.sleep(delay)
        if doc is not None:
            report = handoff_state.apply(router, doc)
            if not report.applied:
                router.failover_blocked = report.why
                return
        if problem := await _takeover_problem(router, report):
            router.failover_blocked = problem
            return
        if not lease.valid():
            router.failover_blocked = "lease expired while applying handoff"
            return
        router.lease_epoch = lease.epoch
        router.lease_holder = lease.holder
        from .lifecycle.identity import allow_profile_recovery, check_process_identities

        router.lifecycle.identities_ready = False
        router.controller.clear_prefill_risk()
        router.controller.clear_decode_risk()
        router.control_wakeup.clear()
        router.standby = False
        try:
            if router.cfg.engine_contract is None:
                for instance in router.scheduler.live_instances():
                    await allow_profile_recovery(router, instance.iid)
                router.lifecycle.identities_ready = True
            else:
                await check_process_identities(router)
        except BaseException:
            router.standby = True
            router.failover_blocked = "identity validation interrupted"
            raise
        if not lease.valid():
            router.standby = True
            router.failover_blocked = "lease expired during identity validation"
            return
        router.failover_blocked = ""
        router.takeover_gap_s = time.monotonic() - last_good
        if doc is None:
            log.warning(
                "COLD START: claimed epoch %d after %d failed probes with no fresh handoff; "
                "serving the configured engine set; %.2fs since last good answer",
                lease.epoch,
                misses,
                router.takeover_gap_s,
            )
        else:
            log.warning(
                "TAKEOVER: claimed epoch %d after %d failed probes; "
                "applied run %s with %d roles; %.2fs since last good answer",
                lease.epoch,
                misses,
                report.run,
                report.roles_applied,
                router.takeover_gap_s,
            )
    finally:
        if router.standby:
            await asyncio.to_thread(lease.release)
