"""Adjacent-split reactive policy and its sustained-demand confirmation state."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from ..types import Instance, Role
from .demand import Demand, OutputEstimates, rounded
from .scoring import PrefillProjection, SplitScore, decision_details

if TYPE_CHECKING:
    from .controller import PrefillRecovery, ReactiveController

Evaluation = tuple[SplitScore, float, float, bool, bool, bool, bool]


def outside_tolerance(recent: float, window: float, tolerance: float) -> bool:
    """Return whether the smaller demand is more than the tolerance below the larger."""
    return abs(recent - window) > tolerance * max(recent, window)


@dataclass(frozen=True)
class ShortView:
    """Current and adjacent splits scored on demand over the confirmation span."""

    demand: Demand
    current: SplitScore
    adjacent: dict[int, SplitScore]


@dataclass(frozen=True)
class Departure:
    """A move away from a settled split after a confirmation-span demand shift."""

    heading: int
    started_at: float
    moved: bool = False


class ReactivePolicy:
    """Choose one adjacent split while the controller coordinates safety and timing."""

    def __init__(self) -> None:
        self.proposal: tuple[int, bool, str] | None = None
        self.confirmations = 0
        # Start of the current run of evaluations in which no adjacent split scores better.
        self.settled_since: float | None = None
        # Start of the current run of agreeing confirmation-span and window demand.
        self.balanced_since: float | None = None
        self.departure: Departure | None = None

    def interrupt(self) -> None:
        """Restart the settled and steady runs while role control is paused."""
        self.settled_since = self.balanced_since = None

    def step(
        self,
        controller: ReactiveController,
        *,
        prefill_recovery: PrefillRecovery | None = None,
        advance_cadence: bool = True,
    ) -> Instance | None:
        """Evaluate one adjacent role change on the cadence or an urgent wake."""
        now = controller._clock()
        adjusted = controller.restore_floor()
        if adjusted:
            controller._last_step = now
            flip = controller.scheduler.flips[-1]
            return controller.monitor.instances.get(flip.iid)
        observed_load = (
            controller.scheduler.pool_load(Role.PREFILL),
            controller.scheduler.pool_load(Role.DECODE),
        )
        controller.scheduler.observe_control_load(*observed_load)
        if now - controller._last_step < controller.step_s and prefill_recovery is None:
            return None
        if advance_cadence:
            controller._last_step = now

        # Resident requests keep their serving engine after reassignment.
        # Splits cover the live engines; a role left without one holds control.
        if any(
            controller.monitor.pool(role) and not controller.scheduler.live_instances(role)
            for role in (Role.PREFILL, Role.DECODE)
        ):
            self.proposal, self.confirmations = None, 0
            self.settled_since = self.balanced_since = None
            current_p = len(controller.monitor.pool(Role.PREFILL))
            n = len(controller.monitor.instances)
            proposed_p = min(current_p + 1, n) if prefill_recovery is not None else current_p
            health_details = None
            if prefill_recovery is not None:
                health_details = {
                    **prefill_recovery.details(now),
                    "current_prefill": current_p,
                    "current_decode": n - current_p,
                    "decision_basis": "projected_ttft_recovery",
                    "eligibility_rule": "projected_ttft_recovery",
                }
            controller.scheduler.record_decision(
                prefill=proposed_p,
                decode=n - proposed_p,
                by="reactive",
                reason="fleet health is changing",
                result="held",
                details=health_details,
            )
            return None

        controller.scorer.recheck(controller.monitor.waiting.values())
        estimates = controller.demand.refresh_output_estimates()
        correction = controller.demand._decode_correction()
        prefill, decode = controller._demand(now, estimates=estimates, correction=correction)
        demand = Demand(
            prefill,
            decode,
            controller.demand.arrival_count(),
            controller.demand.observed_decode.count(),
            controller.last_demand.complete,
        )
        controller.last_demand = demand
        snapshot = controller.scorer.capture(
            now,
            demand,
            utilization=controller.utilization,
            observed_load=observed_load,
            estimates=estimates,
            correction=correction,
            window_s=controller.window_s,
            step_s=controller.step_s,
        )
        n = snapshot.current_prefill + snapshot.current_decode
        current_p = snapshot.current_prefill
        current = snapshot.score(current_p)
        # Incomplete demand selects recovery, which acts on sustained destination pressure.
        recovery = not demand.complete
        observed_prefill, observed_decode = observed_load
        # Recovery evidence holds while known prefill backlog exceeds the pool's latency budget.
        recovery_prefill = snapshot.prefill_recovery_ratio
        recovery_ready = (
            max(recovery_prefill, snapshot.decode_recovery_ratio) >= controller.scheduler.th.expand
            and snapshot.profiles_complete
        )
        recovery_projection = prefill_recovery.projection if prefill_recovery is not None else None
        urgent_ready = recovery_projection is not None and snapshot.profiles_complete
        if (prefill_recovery is not None and not snapshot.profiles_complete) or (
            not urgent_ready
            and (
                demand.arrivals < controller.min_arrivals
                or (not recovery and prefill + decode < controller.demand_floor)
                or (recovery and not recovery_ready)
            )
        ):
            self.proposal, self.confirmations = None, 0
            if prefill_recovery is None:
                self.settled_since = self.balanced_since = None
            details: dict[str, object] = {
                "prefill_work": rounded(demand.prefill_engines),
                "decode_work": rounded(demand.decode_engines),
                "arrivals": demand.arrivals,
                "output_observations": demand.output_observations,
                "demand_complete": demand.complete,
                "observed_prefill_ratio": rounded(observed_prefill),
                "recovery_prefill_ratio": rounded(recovery_prefill),
                "queued_prefill_s": rounded(snapshot.queued_prefill_s),
                "observed_decode_ratio": rounded(observed_decode),
                "recovery_decode_ratio": rounded(snapshot.decode_recovery_ratio),
            }
            if prefill_recovery is not None:
                details.update(prefill_recovery.details(now))
                details["current_prefill"] = current_p
                details["current_decode"] = n - current_p
                details["decision_basis"] = "projected_ttft_recovery"
                details["eligibility_rule"] = "projected_ttft_recovery"
            proposed_p = min(current_p + 1, n) if prefill_recovery is not None else current_p
            controller.scheduler.record_decision(
                prefill=proposed_p,
                decode=n - proposed_p,
                by="reactive",
                reason=(
                    "projected TTFT recovery requires fleet profiles"
                    if prefill_recovery is not None
                    else (
                        "incomplete demand requires observed phase pressure and fleet profiles"
                        if recovery
                        else "insufficient demand history"
                    )
                ),
                result="held",
                details=details,
            )
            return None

        # D-to-P candidates price decode at the larger of the short-horizon and window estimates.
        evidence = controller.safety.capture(now, estimates=estimates, correction=correction)
        envelope_demand = Demand(
            demand.prefill_engines,
            evidence.envelope_decode_engines,
            demand.arrivals,
            demand.output_observations,
            demand.complete,
        )
        th = controller.scheduler.th
        # The confirmation span; steady load prices both phases alike over it and the window.
        confirmation_s = controller.step_s * max(
            controller.confirmations_needed, th.sustained_intervals
        )
        steady_work: tuple[float, float] | None = None
        # Urgent evaluations leave the settled and steady runs and the departure unchanged.
        if not urgent_ready:
            if not recovery:
                steady_work = controller._demand(
                    now, horizon_s=confirmation_s, estimates=estimates, correction=correction
                )
            tolerance = controller.safety.demand_rise_tolerance
            if steady_work is None or any(
                outside_tolerance(recent, window, tolerance)
                for recent, window in (
                    (steady_work[0], demand.prefill_engines),
                    (steady_work[1], demand.decode_engines),
                )
            ):
                self.balanced_since = None
            elif self.balanced_since is None:
                self.balanced_since = now
        # Steady load has stayed balanced for one evidence span.
        steady_s = (
            now - self.balanced_since
            if self.balanced_since is not None and not urgent_ready
            else None
        )
        steady = (
            evidence.closed
            and steady_s is not None
            and steady_s >= controller.safety.evidence_span_s
        )
        candidate_prefills = (current_p + 1,) if urgent_ready else (current_p - 1, current_p + 1)
        adjacent = [
            snapshot.score(
                candidate_p,
                envelope_demand if candidate_p > current_p else None,
            )
            for candidate_p in candidate_prefills
            if 0 < candidate_p < n
            and (
                urgent_ready
                or not recovery
                or (recovery_prefill if candidate_p > current_p else snapshot.decode_recovery_ratio)
                >= controller.scheduler.th.expand
            )
        ]
        short = (
            None
            if urgent_ready or recovery
            else self._short_view(
                controller, now, current_p, confirmation_s, observed_load, estimates, correction
            )
        )
        if not urgent_ready:
            self._track_departure(controller, now, current, adjacent, demand, short)
        departure = self.departure
        short_current = short.current if short is not None else current
        short_demand = short.demand if short is not None else demand
        # After its move, a departure holds the reverse until the window covers it.
        held_reverse = {
            candidate.prefill
            for candidate in adjacent
            if departure is not None
            and departure.moved
            and candidate.prefill - current_p == -departure.heading
        }
        horizons: dict[str, object] = {
            "steady_horizon_s": rounded(confirmation_s),
            "steady_prefill_work": rounded(steady_work[0]) if steady_work is not None else None,
            "steady_decode_work": rounded(steady_work[1]) if steady_work is not None else None,
            "steady_demand_s": rounded(steady_s) if steady_s is not None else None,
        }
        if departure is not None:
            horizons["departure_age_s"] = rounded(now - departure.started_at)

        urgent_candidate: PrefillProjection | None = None
        if urgent_ready and recovery_projection is not None:
            focus = controller.monitor.waiting.get(recovery_projection.rid)
            projections = [
                projection
                for donor in controller.scheduler.live_instances(Role.DECODE)
                if (
                    projection := controller.scorer.project_prefill(
                        now,
                        focus,
                        additional_prefill=(donor,),
                    )
                )
                is not None
            ]
            if projections:
                # The worst donor projection applies; the scheduler's guards choose the donor.
                urgent_candidate = max(
                    projections,
                    key=lambda projection: projection.projected_ttft_s,
                )

        def evaluate(candidate: SplitScore, base: SplitScore) -> Evaluation:
            return (
                candidate,
                (
                    max(candidate.tpot_ratio, candidate.decode_queue_ratio)
                    if candidate.prefill > current_p
                    else candidate.ttft_ratio
                ),
                base.objective - candidate.objective,
                (
                    candidate.decode_kv_capacity_tokens is None
                    or candidate.decode_tokens_per_engine <= candidate.decode_kv_capacity_tokens
                    or candidate.prefill < current_p
                ),
                candidate.decode_profile_covered,
                (
                    base is current
                    and candidate.prefill > current_p
                    and max(candidate.tpot_ratio, candidate.decode_queue_ratio)
                    > controller.scheduler.th.shrink
                    and (recovery_prefill >= controller.scheduler.th.expand or urgent_ready)
                ),
                controller.within_floors(candidate.prefill),
            )

        window_rows = {c.prefill: evaluate(c, current) for c in adjacent}
        # Before its move, a departure prices its heading over the confirmation span.
        # Mixed pressure keeps window pricing.
        departure_moves = {
            p: score
            for p, score in (short.adjacent.items() if short is not None else ())
            if departure is not None
            and not departure.moved
            and p - current_p == departure.heading
            and p in window_rows
            and not window_rows[p][5]
        }
        evaluations: list[Evaluation] = [
            evaluate(departure_moves[p], short_current) if p in departure_moves else row
            for p, row in window_rows.items()
        ]

        def rule(row: Evaluation) -> str:
            candidate, source_ratio, mixed = row[0], row[1], row[5]
            if urgent_ready:
                return "projected_ttft_recovery"
            if mixed:
                return "mixed_pressure"
            if candidate.prefill in departure_moves:
                return "settled_departure"
            # Steady load compares adjacent splits on the objective alone.
            if steady and th.shrink < source_ratio <= th.expand:
                return "steady_demand"
            return "source_shrink"

        def source_safe(row: Evaluation) -> bool:
            return row[1] <= th.shrink or row[5] or rule(row) == "steady_demand"

        def admitted(row: Evaluation) -> bool:
            candidate, source_ratio, improvement, capacity_safe, covered, mixed, floors = row
            if not (
                capacity_safe
                and covered
                and floors
                and (candidate.prefill < current_p or evidence.allowed)
                and candidate.prefill not in held_reverse
            ):
                return False
            if urgent_ready and recovery_projection is not None:
                ttft_improvement = (
                    recovery_projection.projected_ttft_s - urgent_candidate.projected_ttft_s
                    if urgent_candidate is not None
                    else 0.0
                )
                return ttft_improvement > 0.0 and (
                    (mixed and improvement > 0.0)
                    or (not mixed and source_ratio <= controller.scheduler.th.shrink)
                )
            return (
                source_safe(row)
                and improvement >= controller.movement_margin
                and (not mixed or improvement > 0.0)
                and (not mixed or snapshot.profiles_complete)
            )

        def describe(row: Evaluation) -> dict[str, object]:
            candidate, _, _, capacity_safe, _, _, floors_safe = row
            departure_move = candidate.prefill in departure_moves
            details = (
                decision_details(short_current, candidate, short_demand)
                if departure_move
                else decision_details(
                    current,
                    candidate,
                    envelope_demand if candidate.prefill > current_p else demand,
                )
            )
            if candidate.prefill > current_p:
                details.update(evidence.details())
            details["eligibility_rule"] = rule(row)
            details["demand_horizon_s"] = rounded(
                confirmation_s if departure_move else controller.window_s
            )
            details.update(horizons)
            details["observed_prefill_ratio"] = rounded(observed_prefill)
            details["recovery_prefill_ratio"] = rounded(recovery_prefill)
            details["queued_prefill_s"] = rounded(snapshot.queued_prefill_s)
            details["observed_decode_ratio"] = rounded(observed_decode)
            details["decode_capacity_safe"] = capacity_safe
            details["role_floors_safe"] = floors_safe
            details["source_pressure_safe"] = source_safe(row)
            if prefill_recovery is not None:
                details.update(prefill_recovery.details(now))
                details["decision_basis"] = "projected_ttft_recovery"
                if urgent_candidate is not None:
                    details.update(
                        {
                            "candidate_projected_ttft_s": rounded(
                                urgent_candidate.projected_ttft_s
                            ),
                            "candidate_projected_ttft_ratio": rounded(urgent_candidate.ratio),
                            "projected_ttft_improvement_s": rounded(
                                prefill_recovery.projection.projected_ttft_s
                                - urgent_candidate.projected_ttft_s
                            ),
                        }
                    )
            return details

        eligible = [row for row in evaluations if admitted(row)]
        if not eligible:
            self.proposal, self.confirmations = None, 0
            held: Evaluation = (
                min(evaluations, key=lambda row: (row[0].objective, row[0].prefill))
                if evaluations
                else (current, 0.0, 0.0, True, current.decode_profile_covered, False, True)
            )
            (
                candidate,
                _,
                improvement,
                capacity_safe,
                profile_covered,
                mixed_pressure,
                floors_safe,
            ) = held
            if not floors_safe:
                floor = "min_decode" if candidate.prefill > current_p else "min_prefill"
                reason = f"{floor} blocks the adjacent split"
            elif candidate.prefill in held_reverse:
                toward = "decode" if candidate.prefill > current_p else "prefill"
                reason = f"departure toward {toward} holds the reverse move"
            elif candidate.prefill > current_p and not evidence.allowed:
                reason = evidence.reason
            elif mixed_pressure and not snapshot.profiles_complete:
                reason = "mixed pressure requires fleet profiles"
            elif (
                urgent_ready
                and recovery_projection is not None
                and (
                    urgent_candidate is None
                    or urgent_candidate.projected_ttft_s >= recovery_projection.projected_ttft_s
                )
            ):
                reason = "projected TTFT recovery requires a strict prefill improvement"
            elif not profile_covered:
                reason = "decode profile does not cover proposed work"
            elif not capacity_safe:
                reason = "projected decode work exceeds KV capacity"
            elif urgent_ready and mixed_pressure and improvement <= 0.0:
                reason = "mixed pressure requires a lower projected objective"
            elif not source_safe(held):
                source = "decode" if candidate.prefill > current_p else "prefill"
                reason = f"projected {source} pressure blocks consolidation"
            elif improvement < controller.movement_margin or (mixed_pressure and improvement <= 0):
                reason = "projected improvement is below the movement margin"
            else:
                reason = "operator constraints leave no adjacent split"
            controller.scheduler.record_decision(
                prefill=candidate.prefill,
                decode=candidate.decode,
                by="reactive",
                reason=reason,
                result="held",
                details=describe(held),
            )
            return None

        chosen = min(eligible, key=lambda row: (row[0].objective, row[0].prefill))
        candidate = chosen[0]
        eligibility_rule = rule(chosen)
        candidate_p = candidate.prefill
        direction = 1 if candidate_p > current_p else -1
        details = describe(chosen)
        destination_ratio = (
            current.ttft_ratio
            if direction > 0
            else max(current.tpot_ratio, current.decode_queue_ratio)
        )
        # Relaxing shrink requires repeated pressure, even with complete demand.
        # The settled run confirms a departure.
        confirmations = (
            1
            if urgent_ready
            or eligibility_rule == "settled_departure"
            or (
                not recovery
                and eligibility_rule == "source_shrink"
                and destination_ratio >= controller.scheduler.th.expand
            )
            else max(
                controller.confirmations_needed,
                controller.scheduler.th.sustained_intervals,
            )
        )
        proposal = candidate_p, recovery, eligibility_rule
        if proposal != self.proposal:
            self.proposal = proposal
            self.confirmations = 1
        else:
            self.confirmations += 1
        details["confirmations"] = self.confirmations
        details["required_confirmations"] = confirmations
        if self.confirmations < confirmations:
            controller.scheduler.record_decision(
                prefill=candidate.prefill,
                decode=candidate.decode,
                by="reactive",
                reason="waiting for sustained demand",
                result="held",
                details=details,
            )
            return None

        self.proposal, self.confirmations = None, 0
        self.settled_since = None
        target = Role.PREFILL if direction > 0 else Role.DECODE
        moved = controller.scheduler.flip(target, "reactive", decision_details=details)
        if moved is not None and departure is not None:
            if candidate_p in departure_moves:
                # A departure moves one engine; the window paces the rest.
                self.departure = replace(departure, moved=True)
            elif departure.moved and direction == departure.heading:
                # A window move along the heading ends the reverse hold.
                self.departure = None
        if moved is not None and target is Role.DECODE:
            # A move to decode re-arms the consolidation evidence window.
            controller.safety.note_risk_event("p_to_d_recovery", at=now)
        return moved

    @staticmethod
    def _short_view(
        controller: ReactiveController,
        now: float,
        current_p: int,
        short_s: float,
        observed_load: tuple[float, float],
        estimates: OutputEstimates,
        correction: float,
    ) -> ShortView | None:
        """Score the current and adjacent splits on demand over `short_s` seconds.

        Residency covers the latest step.
        """
        arrivals = controller.demand.arrival_count(now - short_s)
        if arrivals < controller.min_arrivals:
            return None
        prefill, decode = controller.demand.estimate(
            now,
            window_s=short_s,
            step_s=controller.step_s,
            horizon_s=short_s,
            estimates=estimates,
            correction=correction,
            resident_s=controller.step_s,
        )
        demand = Demand(
            prefill,
            decode,
            arrivals,
            controller.last_demand.output_observations,
            controller.last_demand.complete,
        )
        snapshot = controller.scorer.capture(
            now,
            demand,
            utilization=controller.utilization,
            observed_load=observed_load,
            estimates=estimates,
            correction=correction,
            window_s=short_s,
            step_s=controller.step_s,
            resident_s=controller.step_s,
            prefills=range(current_p - 1, current_p + 2),
        )
        n = snapshot.current_prefill + snapshot.current_decode
        return ShortView(
            demand,
            snapshot.score(current_p),
            {
                p: snapshot.score(p)
                for p in (current_p - 1, current_p + 1)
                if 0 < p < n and controller.within_floors(p)
            },
        )

    def _track_departure(
        self,
        controller: ReactiveController,
        now: float,
        current: SplitScore,
        adjacent: list[SplitScore],
        demand: Demand,
        short: ShortView | None,
    ) -> None:
        """Track the settled run and open a departure when the confirmation span leaves it."""
        margin = controller.movement_margin
        window_gain = max(
            (
                current.objective - c.objective
                for c in adjacent
                if controller.within_floors(c.prefill)
            ),
            default=0.0,
        )
        heading = 0
        shifted = False
        if short is not None and short.adjacent:
            best = min(short.adjacent.values(), key=lambda c: (c.objective, c.prefill))
            if short.current.objective - best.objective >= margin:
                heading = 1 if best.prefill > current.prefill else -1
            # Confirmation-span demand outside the rise tolerance of window demand marks a shift.
            tolerance = controller.safety.demand_rise_tolerance
            shifted = any(
                outside_tolerance(recent, window, tolerance)
                for recent, window in (
                    (short.demand.prefill_engines, demand.prefill_engines),
                    (short.demand.decode_engines, demand.decode_engines),
                )
            )
        departure = self.departure
        if departure is not None and (
            now - departure.started_at >= controller.window_s
            or (not departure.moved and heading != departure.heading)
        ):
            self.departure = None
        if (
            self.departure is None
            and heading
            and shifted
            and self.settled_since is not None
            and now - self.settled_since >= controller.safety.evidence_span_s
        ):
            self.departure = Departure(heading, now)
        if heading or window_gain >= margin:
            self.settled_since = None
        elif self.settled_since is None:
            self.settled_since = now
