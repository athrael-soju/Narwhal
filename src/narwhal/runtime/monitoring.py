"""Fleet monitoring stages, liveness sweeps, and engine readmission."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..types import Instance, Role
from . import state as handoff_state
from .lifecycle import (
    ValidationOutcome,
    check_process_identities,
    validate_readmission,
)
from .standby import controls_fleet

if TYPE_CHECKING:
    from ..serving.router import NarwhalRouter

log = logging.getLogger("narwhal.monitoring_loop")

MONITOR_STAGES = (
    "controller",
    "health",
    "drains",
    "rollover",
    "readmission",
    "liveness",
    "handoff",
    # Floor-state refresh and loop logging can fail independently of the
    # controller. Account for those failures without stopping the monitor loop.
    "telemetry",
)


@dataclass
class MonitoringStage:
    """Failure ledger for one monitoring stage.

    The exception class is retained without its message: log lines are local,
    but state, metrics, and the journal travel and messages can embed
    endpoint or request data.
    """

    failures: int = 0
    consecutive: int = 0
    last_class: str | None = None
    last_at: float | None = None


class MonitoringLedger:
    """Per-stage failure accounting for the monitoring loop.

    Every guarded stage records its own outcome. The core streak increments
    once per pass in which any stage failed and resets only after a pass with
    zero stage failures; at `monitor_failure_limit` consecutive failed passes
    the router stops admitting new requests until a fully clean pass. The
    counters are process-local: standby takeover starts them at zero.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        on_event: Callable[[dict], None] | None = None,
    ) -> None:
        self._clock = clock
        self._on_event = on_event
        self.stages = {name: MonitoringStage() for name in MONITOR_STAGES}
        self.core_consecutive = 0
        self.core_failures = 0
        # The degraded reason pins the first failure of the streak that
        # crossed the limit, as `<class>:<stage>`.
        self.degraded = ""
        self._streak_first: tuple[str, str] | None = None
        self._pass_failures: list[tuple[str, str]] = []
        self.event_loop_lag_s = 0.0
        self.event_loop_lag_high_water_s = 0.0

    def observe_event_loop_lag(self, lag_s: float) -> None:
        """Record delay beyond one scheduled monitoring deadline."""
        self.event_loop_lag_s = max(0.0, lag_s)
        self.event_loop_lag_high_water_s = max(
            self.event_loop_lag_high_water_s, self.event_loop_lag_s
        )

    def _event(self, row: dict[str, Any]) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(row)
        except Exception:
            # Keep monitoring active after a journal write fails.
            log.exception("monitoring journal event write failed")

    def ok(self, stage: str) -> None:
        """Record one successful stage execution."""
        self.stages[stage].consecutive = 0

    def fail(self, stage: str, exc: BaseException) -> None:
        """Record one stage failure, journal it, and log with the traceback."""
        record = self.stages[stage]
        klass = type(exc).__name__
        record.failures += 1
        record.consecutive += 1
        record.last_class = klass
        record.last_at = self._clock()
        self._pass_failures.append((stage, klass))
        log.exception("monitoring pass: %s stage failed (%s)", stage, klass)
        self._event(
            {
                "event": "monitoring_stage_failure",
                "at": record.last_at,
                "stage": stage,
                "class": klass,
                "consecutive": record.consecutive,
            }
        )

    def finish_pass(self, limit: int) -> None:
        """Close one pass: update the core streak and the degraded gate."""
        failed = self._pass_failures
        self._pass_failures = []
        if not failed:
            self.core_consecutive = 0
            self._streak_first = None
            if self.degraded:
                self.degraded = ""
                self._event({"event": "monitoring_recovered", "at": self._clock()})
            return
        if self._streak_first is None:
            self._streak_first = failed[0]
        self.core_consecutive += 1
        self.core_failures += 1
        if not self.degraded and self.core_consecutive >= limit:
            stage, klass = self._streak_first
            self.degraded = f"{klass}:{stage}"
            self._event(
                {
                    "event": "monitoring_degraded",
                    "at": self._clock(),
                    "stage": stage,
                    "class": klass,
                    "core_consecutive": self.core_consecutive,
                }
            )

    def snapshot(self) -> dict[str, Any]:
        """Render the ledger for `/narwhal/state`."""
        return {
            "degraded": bool(self.degraded),
            "reason": self.degraded or None,
            "core_consecutive": self.core_consecutive,
            "core_failures": self.core_failures,
            "event_loop_lag_s": self.event_loop_lag_s,
            "event_loop_lag_high_water_s": self.event_loop_lag_high_water_s,
            "stages": {
                name: {
                    "failures": record.failures,
                    "consecutive": record.consecutive,
                    "last_class": record.last_class,
                    "last_at": record.last_at,
                }
                for name, record in self.stages.items()
            },
        }


async def readmit(router: NarwhalRouter, after_s: float) -> list[str]:
    """Validate and readmit due ejected engines whose health probe succeeds.

    Ejected engines receive no traffic, so probes provide their recovery path.
    A declared engine contract requires the same attestation, generation, and
    fabric gates used by planned restarts.
    """
    if router.cfg.engine_restart_policy == "whole_wave" and router.scheduler.ejected:
        async with router.lifecycle.lock:
            router.lifecycle.require_restart_wave("an engine was excluded")
        return []
    for iid in sorted(router.scheduler.inference_suspects):
        if iid in router.scheduler.ejected or iid in router.scheduler.draining:
            continue
        key = (iid, "verify_inference")
        if key not in router.scheduler.verifying and (
            router._clock() - router._verification_at.get(iid, 0.0) >= after_s
        ):
            router.scheduler.verifying.add(key)
            router._start_verification(iid, "verify_inference")
    due = router.scheduler.probe_due(after_s)
    if not due:
        return []
    # A total outage needs peers for contract validation. Probe the cohort
    # together even when individual ejection times have different cadences.
    recovery_wave = router.cfg.engine_contract is not None and not router.scheduler.live_instances()
    if recovery_wave:
        due = router.scheduler.probe_due(0.0)
        if set(due) != set(router.monitor.instances):
            return []
    answers = await asyncio.gather(
        *(router.engines.healthy(router.monitor.instances[iid].url) for iid in due),
        return_exceptions=True,
    )
    # None answers are inconclusive (the local control pool waited out) and
    # fall out of the `is True` filter beside False.
    healthy = [iid for iid, ok in zip(due, answers, strict=True) if ok is True]
    back: list[str] = []
    # KV transfer validation needs live peers. After a total outage, wait
    # for the complete fleet and validate it atomically.
    if recovery_wave and set(healthy) != set(router.monitor.instances):
        return []
    groups = [healthy] if recovery_wave else [[iid] for iid in healthy]
    for engines in groups:
        iid = engines[0]
        if router.cfg.engine_contract is None:
            if iid in router.scheduler.inference_suspects:
                await router._verify_inference(iid, router.monitor.instances[iid].url)
                if iid not in router.scheduler.ejected:
                    back.append(iid)
                continue
            # Health-only readmission clears the health-evidence classes;
            # inference classes wait on request-path or probe evidence.
            router.scheduler.record_answer(iid, "health")
            back.append(iid)
            log.info("readmitted %s: /health answered", iid)
            continue
        async with router.lifecycle.lock:
            if not router.lifecycle.start_recovery_validation(engines, wave=recovery_wave):
                continue
            try:
                outcome = await validate_readmission(
                    router,
                    engines,
                    wave=recovery_wave,
                    transport=router.lifecycle_transport,
                )
            except Exception as exc:
                log.exception("automatic readmission validation failed for %s", iid)
                outcome = ValidationOutcome()
                outcome.fail(iid, f"validation raised {type(exc).__name__}: {exc}")
            if not controls_fleet(router):
                outcome.fail(iid, "router control was fenced during validation")
            if outcome.passed and set(engines).issubset(outcome.starts):
                router.lifecycle.readmitted(engines, outcome)
                back.extend(engines)
                log.info("readmitted %s: recovery validation passed", ", ".join(engines))
            else:
                router.lifecycle.validation_failed(outcome)
                log.warning("held %s out: recovery validation failed", iid)
    return back


async def sweep_liveness(router: NarwhalRouter) -> list[str]:
    """Eject live engines after consecutive failed health sweeps.

    Sweeps cover idle periods when request failures provide no breaker evidence.
    """
    misses = router.scheduler.liveness_misses
    live = [
        iid
        for iid in router.monitor.instances
        if iid not in router.scheduler.ejected and iid not in router.scheduler.draining
    ]
    if not live:
        return []
    answers = await asyncio.gather(
        *(router.engines.healthy(router.monitor.instances[iid].url) for iid in live),
        return_exceptions=True,
    )
    gone = []
    for iid, ok in zip(live, answers, strict=True):
        if iid in router.scheduler.ejected or iid in router.scheduler.draining:
            continue
        if ok is True:
            # A health 200 is recovery evidence: it clears the
            # health-evidence classes and lifts the quarantine.
            router.scheduler.record_answer(iid, "health")
            continue
        if ok is None:
            # Pool exhaustion leaves the engine's miss counter unchanged.
            log.info("liveness %s: probe waited out the control pool; sweep skipped", iid)
            continue
        misses[iid] = misses.get(iid, 0) + 1
        if misses[iid] < router.cfg.liveness_misses:
            log.info(
                "liveness %s: /health silent (%d of %d)",
                iid,
                misses[iid],
                router.cfg.liveness_misses,
            )
            continue
        if router.scheduler.eject(iid):
            misses.pop(iid, None)
            gone.append(iid)
            log.warning(
                "ejected %s: /health silent on %d consecutive sweeps, no traffic needed",
                iid,
                router.cfg.liveness_misses,
            )
    gone.extend(
        await check_process_identities(
            router, [iid for iid, ok in zip(live, answers, strict=True) if ok is True]
        )
    )
    return list(dict.fromkeys(gone))


def run_controller(
    router: NarwhalRouter,
    *,
    urgent: bool = False,
    scheduled: bool = True,
) -> Instance | None:
    """Run one serialized controller evaluation.

    Urgent evaluations preserve the periodic demand-sampling cadence while
    allowing a revalidated prefill-risk event through the step timer.
    """
    below_floor = router.scheduler.decode_live() < router.scheduler.min_decode
    out: Instance | None = None
    if below_floor:
        out = router.scheduler.restore_decode_floor()
    if out is not None:
        # Decode-floor recovery starts a fresh consolidation evidence window.
        router.controller.safety.note_risk_event("p_to_d_recovery")
    if scheduled:
        router.controller.sample()
    if not below_floor:
        out = router.controller.step(urgent=urgent, scheduled=scheduled)
    return out


async def urgent_control_once(router: NarwhalRouter) -> Instance | None:
    """Evaluate one coalesced prefill-risk event between monitoring passes."""
    flipped: Instance | None = None
    try:
        flipped = run_controller(router, urgent=True, scheduled=False)
    except Exception as exc:
        router.monitoring.fail("controller", exc)
    else:
        router.monitoring.ok("controller")
    if flipped is not None:
        try:
            handoff_state.write(router.cfg.state_path, handoff_state.snapshot(router))
        except Exception as exc:
            router.monitoring.fail("handoff", exc)
        else:
            router.monitoring.ok("handoff")
    if router.controller.prefill_risk_pending:
        router.control_wakeup.set()
    router.dispatcher.notify()
    router.admission_queue.notify()
    return flipped


async def monitor_once(router: NarwhalRouter, *, urgent: bool = False) -> Instance | None:
    """Run one controller, health, readmission and telemetry pass.

    Each stage records its failures and the pass continues. A pass with
    zero stage failures resets the core failure streak.
    """
    interval = router.cfg.monitor_interval_s
    flipped: Instance | None = None

    try:
        flipped = run_controller(router, urgent=urgent)
    except Exception as exc:
        router.monitoring.fail("controller", exc)
    else:
        router.monitoring.ok("controller")
    # Health and cleanup must run even when the controller fails.
    for stage, body in (
        ("health", router.scheduler.health_pass),
        ("drains", router.scheduler.settle_drains),
        ("rollover", router.monitor.roll_interval),
    ):
        try:
            body()
        except Exception as exc:
            router.monitoring.fail(stage, exc)
        else:
            router.monitoring.ok(stage)
    try:
        await readmit(router, interval * router.cfg.readmit_every)
    except Exception as exc:
        router.monitoring.fail("readmission", exc)
    else:
        router.monitoring.ok("readmission")
    if router.cfg.liveness_every and router.scheduler.sweep_due(
        interval * router.cfg.liveness_every
    ):
        try:
            await sweep_liveness(router)
        except Exception as exc:
            router.monitoring.fail("liveness", exc)
        else:
            router.monitoring.ok("liveness")
    try:
        router.scheduler.refresh_floor_state()
        lp = router.scheduler.pool_load(Role.PREFILL)
        ld = router.scheduler.pool_load(Role.DECODE)
        n_prefill = len(router.monitor.pool(Role.PREFILL))
        n_decode = len(router.monitor.pool(Role.DECODE))
        log.info(
            "loop | Lp=%.3f Ld=%.3f | %dP%dD | unserved=%d",
            lp,
            ld,
            n_prefill,
            n_decode,
            router.scheduler.unserved,
        )
    except Exception as exc:
        router.monitoring.fail("telemetry", exc)
    else:
        router.monitoring.ok("telemetry")
    # Limit handoff staleness to one monitoring interval: standby takeover
    # needs fresh handoffs, so a degraded primary still publishes.
    try:
        handoff_state.write(router.cfg.state_path, handoff_state.snapshot(router))
    except Exception as exc:
        router.monitoring.fail("handoff", exc)
    else:
        router.monitoring.ok("handoff")
    router.monitoring.finish_pass(router.cfg.monitor_failure_limit)
    if router.controller.prefill_risk_pending:
        router.control_wakeup.set()
    router.dispatcher.notify()
    router.admission_queue.notify()
    return flipped


async def monitor_loop(router: NarwhalRouter) -> None:
    """Run scheduled monitoring and wake control for prefill SLO risk."""
    loop = asyncio.get_running_loop()
    interval = router.cfg.monitor_interval_s
    next_pass = loop.time() + interval
    while True:
        timeout = max(0.0, next_pass - loop.time())
        try:
            await asyncio.wait_for(router.control_wakeup.wait(), timeout=timeout)
            woke = True
        except TimeoutError:
            woke = False
        if woke:
            router.control_wakeup.clear()
        due = loop.time() >= next_pass
        if due:
            router.monitoring.observe_event_loop_lag(loop.time() - next_pass)
            while next_pass <= loop.time():
                next_pass += interval
        if router.standby or router.lifecycle_blocked:
            # The primary owns standby actuation. A whole wave freezes roles.
            continue
        if due:
            await monitor_once(router, urgent=woke)
        elif woke:
            await urgent_control_once(router)
