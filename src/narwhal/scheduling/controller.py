"""Reactive role control with adjacent-split scoring and floor recovery."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from ..types import Instance, Request, Role
from .consolidation import ConsolidationSafety
from .demand import ArrivalObservation, Demand, DemandModel, OutputEstimates, rounded
from .monitor import InstanceMonitor
from .reactive import ReactivePolicy
from .scheduler import GlobalScheduler
from .scoring import PrefillProjection, SplitScorer

log = logging.getLogger("narwhal.controller")


@dataclass
class PrefillUrgency:
    """Coalesced prefill SLO-risk signals awaiting controller evaluation."""

    signalled_at: float
    initial_projected_ttft_s: float
    signals: int = 1


@dataclass(frozen=True)
class PrefillRecovery:
    """Revalidated prefill SLO risk consumed by one controller evaluation."""

    signalled_at: float
    initial_projected_ttft_s: float
    signals: int
    projection: PrefillProjection

    def details(self, now: float) -> dict[str, object]:
        """Render the urgent trigger fields for a controller decision."""
        return {
            **self.projection.details(),
            "initial_projected_ttft_s": rounded(self.initial_projected_ttft_s),
            "urgent_signals": self.signals,
            "event_to_evaluation_s": rounded(max(0.0, now - self.signalled_at)),
        }


class ReactiveController:
    """Evaluate one role change per step while preserving serving floors."""

    def __init__(
        self,
        monitor: InstanceMonitor,
        scheduler: GlobalScheduler,
        *,
        clock: Callable[[], float],
        window_s: float,
        confirmations: int,
        utilization: float,
        min_arrivals: int,
        demand_floor: float,
        step_s: float = 5.0,
        movement_margin: float = 0.05,
        evidence_span_s: float = 60.0,
        evidence_max_span_s: float = 120.0,
        evidence_min_arrivals: int = 10,
        demand_rise_tolerance: float = 0.25,
    ) -> None:
        self.monitor = monitor
        self.scheduler = scheduler
        self._clock = clock
        self.window_s = window_s
        self.confirmations_needed = confirmations
        self.utilization = utilization
        self.min_arrivals = min_arrivals
        self.demand_floor = demand_floor
        self.step_s = step_s
        self.movement_margin = movement_margin
        self._last_step = clock()
        self.demand = DemandModel(
            monitor,
            scheduler,
            clock,
            window_s=window_s,
            bucket_s=min(1.0, step_s, evidence_span_s),
        )
        self.reactive = ReactivePolicy()
        self.scorer = SplitScorer(self.demand)
        self._prefill_urgency: PrefillUrgency | None = None
        self.safety = ConsolidationSafety(
            self,
            evidence_span_s=evidence_span_s,
            evidence_max_span_s=evidence_max_span_s,
            evidence_min_arrivals=evidence_min_arrivals,
            demand_rise_tolerance=demand_rise_tolerance,
        )

    def _clamp_prefill_target(self, want: int) -> int:
        return max(
            self.scheduler.min_prefill,
            min(want, len(self.monitor.instances) - self.scheduler.min_decode),
        )

    def saw_arrival(
        self, input_len: int, *, wanted_len: int | None = None, at: float | None = None
    ) -> ArrivalObservation:
        """Record offered work before request admission."""
        return self.demand.saw_arrival(input_len, wanted_len=wanted_len, at=at)

    def saw_completion(
        self, input_len: int, wanted_len: int, observed_len: int, *, at: float | None = None
    ) -> None:
        """Record the output delivered by one successful request."""
        self.demand.saw_completion(input_len, wanted_len, observed_len, at=at)

    def note_prefill_risk(self, request: Request, *, at: float | None = None) -> bool:
        """Arm an urgent evaluation when one offered prefill exceeds its projected SLO."""
        seen = self._clock() if at is None else at
        projection = self.scorer.project_prefill(seen, request)
        if projection is None or not projection.breached:
            return False
        if self._prefill_urgency is None:
            self._prefill_urgency = PrefillUrgency(
                signalled_at=seen,
                initial_projected_ttft_s=projection.projected_ttft_s,
            )
        else:
            self._prefill_urgency.initial_projected_ttft_s = max(
                self._prefill_urgency.initial_projected_ttft_s,
                projection.projected_ttft_s,
            )
            self._prefill_urgency.signals += 1
        return True

    def consume_prefill_risk(self, now: float) -> PrefillRecovery | None:
        """Consume one coalesced event after recomputing risk from live queued work."""
        urgency = self._prefill_urgency
        if urgency is None:
            return None
        self._prefill_urgency = None
        projection = self.scorer.project_prefill(now)
        if projection is None or not projection.breached:
            return None
        return PrefillRecovery(
            signalled_at=urgency.signalled_at,
            initial_projected_ttft_s=urgency.initial_projected_ttft_s,
            signals=urgency.signals,
            projection=projection,
        )

    def clear_prefill_risk(self) -> None:
        """Start a new control-ownership epoch with empty urgent state."""
        self._prefill_urgency = None

    @property
    def prefill_risk_pending(self) -> bool:
        """Return whether a coalesced prefill event awaits evaluation."""
        return self._prefill_urgency is not None

    def _rearm_prefill_risk(self, now: float) -> bool:
        """Publish a fresh event when live queued work remains above its SLO."""
        projection = self.scorer.project_prefill(now)
        if projection is None or not projection.breached:
            return False
        self._prefill_urgency = PrefillUrgency(
            signalled_at=now,
            initial_projected_ttft_s=projection.projected_ttft_s,
        )
        return True

    def sample(self) -> None:
        """Sample decode residency for demand estimation."""
        self.demand.sample()

    @property
    def last_demand(self) -> Demand:
        """Most recently estimated offered and resident phase demand."""
        return self.demand.last_demand

    @last_demand.setter
    def last_demand(self, value: Demand) -> None:
        self.demand.last_demand = value

    def _demand(
        self,
        now: float,
        horizon_s: float | None = None,
        *,
        estimates: OutputEstimates | None = None,
        correction: float | None = None,
    ) -> tuple[float, float]:
        return self.demand.estimate(
            now,
            window_s=self.window_s,
            step_s=self.step_s,
            horizon_s=horizon_s,
            estimates=estimates,
            correction=correction,
        )

    def step(self, *, urgent: bool = False, scheduled: bool | None = None) -> Instance | None:
        """Evaluate one adjacent role change on the cadence or an urgent wake."""
        advances_cadence = not urgent if scheduled is None else scheduled
        recovery = (
            self.consume_prefill_risk(self._clock())
            if urgent or self._prefill_urgency is not None
            else None
        )
        moved = self.reactive.step(
            self,
            prefill_recovery=recovery,
            advance_cadence=advances_cadence,
        )
        if recovery is not None and moved is not None and moved.role is Role.PREFILL:
            self._rearm_prefill_risk(self._clock())
        return moved

    def within_floors(self, prefill: int) -> bool:
        """Whether a proposed prefill count preserves both configured floors."""
        return (
            self.scheduler.min_prefill
            <= prefill
            <= (len(self.monitor.instances) - self.scheduler.min_decode)
        )

    def restore_floor(self) -> int:
        """Move healthy decode engines until the live prefill floor is met.

        Preserve the configured decode floor and report any remaining prefill deficit.
        """
        live = self.scheduler.live_instances()
        target = self._clamp_prefill_target(
            min(
                self.scheduler.min_prefill,
                max(0, len(live) - self.scheduler.min_decode),
            ),
        )
        effective_prefill = {inst.iid for inst in self.scheduler.live_instances(Role.PREFILL)}
        moved = 0
        while len(effective_prefill) < target:
            decode = self.scheduler.live_instances(Role.DECODE)
            if len(decode) <= self.scheduler.min_decode:
                break
            pool = [inst for inst in decode if inst.iid not in self.scheduler.pinned]
            if not pool:
                break
            mover = min(pool, key=lambda inst: len(inst.prefill) + len(inst.decode))
            moved_engine = self.scheduler.flip(
                Role.PREFILL,
                "floor_recovery",
                candidate=mover,
                bypass_cooldown=True,
                bypass_dwell=True,
            )
            if moved_engine is None:
                break
            effective_prefill.add(mover.iid)
            moved += 1
            break
        self.scheduler.refresh_floor_state()
        if moved:
            log.warning(
                "CONTROL restored min_prefill with %d recovery move(s): %d live prefill",
                moved,
                self.scheduler.prefill_live(),
            )
        return moved
