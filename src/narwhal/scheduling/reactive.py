"""Adjacent-split reactive policy and its sustained-demand confirmation state."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..types import Instance, Role
from .demand import Demand, rounded
from .scoring import PrefillProjection, SplitScore, decision_details

if TYPE_CHECKING:
    from .controller import PrefillRecovery, ReactiveController

Evaluation = tuple[SplitScore, float, float, bool, bool, bool, bool]


class ReactivePolicy:
    """Choose one adjacent split while the controller coordinates safety and timing."""

    def __init__(self) -> None:
        self.proposal: tuple[int, bool, bool] | None = None
        self.confirmations = 0

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
        # Price them in each subsequent proposal; only unavailable engines,
        # including actual relaunch downtime, block control fleet-wide.
        if len(controller.scheduler.live_instances()) != len(controller.monitor.instances):
            self.proposal, self.confirmations = None, 0
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

        estimates = controller.demand._output_estimates()
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
        # With incomplete demand, use sustained destination pressure for recovery
        # and known demand plus resident/queued work to price the source pool.
        recovery = not demand.complete
        observed_prefill, observed_decode = observed_load
        # Dispatch caps active work even when admitted requests keep waiting.
        # Preserve recovery evidence across active-price dips while known
        # prefill backlog still exceeds the current pool's latency budget.
        recovery_prefill = snapshot.prefill_recovery_ratio
        recovery_ready = (
            max(recovery_prefill, observed_decode) >= controller.scheduler.th.expand
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

        # The D-to-P direction prices the adjacent decode split against the
        # conservative envelope: the short-horizon estimate when it exceeds
        # the window estimate. the snapshot also includes pending in-prefill
        # output work to the candidate's resident decode figures.
        evidence = controller.safety.capture(now, estimates=estimates, correction=correction)
        envelope_demand = Demand(
            demand.prefill_engines,
            evidence.envelope_decode_engines,
            demand.arrivals,
            demand.output_observations,
            demand.complete,
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
                or (recovery_prefill if candidate_p > current_p else observed_decode)
                >= controller.scheduler.th.expand
            )
        ]
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
                # Any source engine may be removed by the scheduler's guards.
                # Require every possible donor profile to improve this request.
                urgent_candidate = max(
                    projections,
                    key=lambda projection: projection.projected_ttft_s,
                )

        evaluations: list[Evaluation] = [
            (
                candidate,
                (
                    max(candidate.tpot_ratio, candidate.decode_queue_ratio)
                    if candidate.prefill > current_p
                    else candidate.ttft_ratio
                ),
                current.objective - candidate.objective,
                (
                    candidate.decode_kv_capacity_tokens is None
                    or candidate.decode_tokens_per_engine <= candidate.decode_kv_capacity_tokens
                    or candidate.prefill < current_p
                ),
                candidate.decode_profile_covered,
                (
                    candidate.prefill > current_p
                    and max(candidate.tpot_ratio, candidate.decode_queue_ratio)
                    > controller.scheduler.th.shrink
                    and (recovery_prefill >= controller.scheduler.th.expand or urgent_ready)
                ),
                controller.within_floors(candidate.prefill),
            )
            for candidate in adjacent
        ]

        def admitted(row: Evaluation) -> bool:
            candidate, source_ratio, improvement, capacity_safe, covered, mixed, floors = row
            if not (
                capacity_safe
                and covered
                and floors
                and (candidate.prefill < current_p or evidence.allowed)
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
                (source_ratio <= controller.scheduler.th.shrink or mixed)
                and improvement >= controller.movement_margin
                and (not mixed or improvement > 0.0)
                and (not mixed or snapshot.profiles_complete)
            )

        eligible = [row for row in evaluations if admitted(row)]
        if not eligible:
            self.proposal, self.confirmations = None, 0
            if evaluations:
                (
                    candidate,
                    source_ratio,
                    improvement,
                    capacity_safe,
                    profile_covered,
                    mixed_pressure,
                    floors_safe,
                ) = min(
                    evaluations,
                    key=lambda row: (
                        row[0].objective,
                        row[0].prefill,
                    ),
                )
            else:
                (
                    candidate,
                    source_ratio,
                    improvement,
                    capacity_safe,
                    profile_covered,
                    mixed_pressure,
                    floors_safe,
                ) = (
                    current,
                    0.0,
                    0.0,
                    True,
                    current.decode_profile_covered,
                    False,
                    True,
                )
            if not floors_safe:
                floor = "min_decode" if candidate.prefill > current_p else "min_prefill"
                reason = f"{floor} blocks the adjacent split"
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
            elif source_ratio > controller.scheduler.th.shrink and not mixed_pressure:
                source = "decode" if candidate.prefill > current_p else "prefill"
                reason = f"projected {source} pressure blocks consolidation"
            elif improvement < controller.movement_margin or (mixed_pressure and improvement <= 0):
                reason = "projected improvement is below the movement margin"
            else:
                reason = "operator constraints leave no adjacent split"
            detail_demand = envelope_demand if candidate.prefill > current_p else demand
            details = decision_details(current, candidate, detail_demand)
            details["eligibility_rule"] = (
                "projected_ttft_recovery"
                if urgent_ready
                else "mixed_pressure"
                if mixed_pressure
                else "source_shrink"
            )
            details["observed_prefill_ratio"] = rounded(observed_prefill)
            details["recovery_prefill_ratio"] = rounded(recovery_prefill)
            details["queued_prefill_s"] = rounded(snapshot.queued_prefill_s)
            details["observed_decode_ratio"] = rounded(observed_decode)
            details["decode_capacity_safe"] = capacity_safe
            details["role_floors_safe"] = floors_safe
            details["source_pressure_safe"] = (
                source_ratio <= controller.scheduler.th.shrink or mixed_pressure
            )
            if candidate.prefill > current_p:
                details.update(evidence.details())
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
            controller.scheduler.record_decision(
                prefill=candidate.prefill,
                decode=candidate.decode,
                by="reactive",
                reason=reason,
                result="held",
                details=details,
            )
            return None

        (
            candidate,
            source_ratio,
            _,
            capacity_safe,
            _,
            mixed_pressure,
            floors_safe,
        ) = min(
            eligible,
            key=lambda row: (
                row[0].objective,
                row[0].prefill,
            ),
        )
        candidate_p = candidate.prefill
        direction = 1 if candidate_p > current_p else -1
        details = decision_details(
            current,
            candidate,
            envelope_demand if direction > 0 else demand,
        )
        if direction > 0:
            details.update(evidence.details())
        details["observed_prefill_ratio"] = rounded(observed_prefill)
        details["recovery_prefill_ratio"] = rounded(recovery_prefill)
        details["queued_prefill_s"] = rounded(snapshot.queued_prefill_s)
        details["observed_decode_ratio"] = rounded(observed_decode)
        details["decode_capacity_safe"] = capacity_safe
        details["role_floors_safe"] = floors_safe
        details["source_pressure_safe"] = (
            source_ratio <= controller.scheduler.th.shrink or mixed_pressure
        )
        details["eligibility_rule"] = (
            "projected_ttft_recovery"
            if urgent_ready
            else "mixed_pressure"
            if mixed_pressure
            else "source_shrink"
        )
        if prefill_recovery is not None:
            details.update(prefill_recovery.details(now))
            details["decision_basis"] = "projected_ttft_recovery"
            if urgent_candidate is not None:
                details.update(
                    {
                        "candidate_projected_ttft_s": rounded(urgent_candidate.projected_ttft_s),
                        "candidate_projected_ttft_ratio": rounded(urgent_candidate.ratio),
                        "projected_ttft_improvement_s": rounded(
                            prefill_recovery.projection.projected_ttft_s
                            - urgent_candidate.projected_ttft_s
                        ),
                    }
                )

        destination_ratio = (
            current.ttft_ratio
            if direction > 0
            else max(current.tpot_ratio, current.decode_queue_ratio)
        )
        # Relaxing decode shrink requires repeated pressure, even with complete
        # demand. Objective margin, evidence and dwell still constrain reversals.
        confirmations = (
            1
            if urgent_ready
            or (
                not recovery
                and not mixed_pressure
                and destination_ratio >= controller.scheduler.th.expand
            )
            else max(
                controller.confirmations_needed,
                controller.scheduler.th.sustained_intervals,
            )
        )
        proposal = candidate_p, recovery, mixed_pressure
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
        target = Role.PREFILL if direction > 0 else Role.DECODE
        moved = controller.scheduler.flip(target, "reactive", decision_details=details)
        if moved is not None and target is Role.DECODE:
            # An applied decode-need move re-arms the evidence window like
            # every other re-split toward decode.
            controller.safety.note_risk_event("p_to_d_recovery", at=now)
        return moved
