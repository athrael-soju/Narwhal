"""Project phase pressure and resident work for a proposed role split."""

from __future__ import annotations

import math
from collections.abc import Collection, Iterable
from dataclasses import dataclass, replace

from ..profiling.model import Profile
from ..types import Instance, Phase, Request, Role
from .demand import Demand, DemandModel, OutputEstimates, rounded
from .prefill import prefill_seconds, resident_prefill_seconds

# Projections reuse a waiting request's cache evidence checked within this many seconds.
CACHE_RECHECK_S = 0.25


@dataclass(frozen=True)
class SplitScore:
    """Predicted SLO pressure after assigning a complete live fleet."""

    prefill: int
    decode: int
    ttft_ratio: float
    tpot_ratio: float
    objective: float
    decode_queue_ratio: float = 0.0
    decode_tokens_per_engine: float = 0.0
    decode_slo_capacity_tokens: float | None = None
    decode_kv_capacity_tokens: float | None = None
    decode_request_limit: int | None = None
    decode_requests_per_engine: float = 0.0
    pending_decode_requests: int = 0
    pending_decode_tokens: float = 0.0
    decode_profile_covered: bool = True
    decode_correction: float = 1.0


@dataclass(frozen=True)
class PrefillProjection:
    """Profile-backed completion estimate for one queued prefill request."""

    rid: str
    at: float
    projected_ttft_s: float
    ttft_slo_s: float
    resident_prefill_s: float
    queued_prefill_s: float
    waiting_prefill: int

    @property
    def ratio(self) -> float:
        """Return projected TTFT as a ratio of the configured SLO."""
        return self.projected_ttft_s / self.ttft_slo_s

    @property
    def breached(self) -> bool:
        """Return whether the projection exceeds the configured TTFT SLO."""
        return self.projected_ttft_s > self.ttft_slo_s

    def details(self) -> dict[str, object]:
        """Render decision fields for the projected-TTFT recovery path."""
        return {
            "trigger_rid": self.rid,
            "projected_ttft_s": rounded(self.projected_ttft_s),
            "ttft_slo_s": rounded(self.ttft_slo_s),
            "trigger_projected_ttft_ratio": rounded(self.ratio),
            "resident_prefill_s": rounded(self.resident_prefill_s),
            "queued_prefill_s": rounded(self.queued_prefill_s),
            "waiting_prefill": self.waiting_prefill,
        }


@dataclass(frozen=True)
class SplitSnapshot:
    """Immutable work and profile inputs shared by every candidate in one decision."""

    at: float
    demand: Demand
    current_prefill: int
    current_decode: int
    profiles: tuple[Profile, ...]
    profiles_complete: bool
    ttft_slo: float
    tpot_slo: float
    utilization: float
    prefill_pressure: float
    decode_pressure: float
    resident_prefill_s: float
    resident_decode_tokens: float
    resident_decode_requests: int
    pending_decode_tokens: float
    pending_decode_requests: int
    pending_decode_shapes: tuple[tuple[int, int], ...]
    decode_correction: float
    resident_profile_covered: bool
    queued_prefill_s: float = 0.0
    active_decode_instances: int = 0
    profile_options: tuple[tuple[int, tuple[Profile, ...]], ...] = ()
    demand_options: tuple[tuple[int, Demand], ...] = ()
    offered_outputs: tuple[int, ...] = ()
    decode_concurrency: int = 0
    waiting_decode_requests: int = 0
    # Decode residents on engines that currently hold the decode role.
    decode_role_requests: int = 0
    # Decode-slot wait over the remaining TTFT budget, by decode engine count.
    decode_wait_ratios: tuple[tuple[int, float], ...] = ()

    @property
    def decode_recovery_ratio(self) -> float:
        """Return the larger of decode pressure and capped decode-slot occupancy."""
        return max(self.decode_pressure, self._decode_slots(self.current_decode))

    def _decode_slots(self, decode: int) -> float:
        """Return the capped decode slots that decode-role residents and waiting work fill.

        A smaller decode pool keeps each departing engine's residents on that engine.
        """
        cap = self.decode_concurrency
        engines = max(decode, self.current_decode)
        if cap <= 0 or decode <= 0:
            return 0.0
        return (self.decode_role_requests + self.waiting_decode_requests) / (engines * cap)

    @property
    def prefill_recovery_ratio(self) -> float:
        """Return prefill pressure, raised by resident prefill work while requests queue."""
        if self.queued_prefill_s <= 0 or self.current_prefill <= 0:
            return self.prefill_pressure
        return max(
            self.prefill_pressure,
            self.resident_prefill_s / (self.current_prefill * self.ttft_slo),
        )

    def score(
        self, prefill: int, demand: Demand | None = None, *, include_resident: bool = True
    ) -> SplitScore:
        """Price a candidate without consulting live state or the clock."""
        decode = self.current_prefill + self.current_decode - prefill
        priced = next(
            (row for candidate_prefill, row in self.demand_options if candidate_prefill == prefill),
            None,
        )
        if demand is None:
            demand = priced or self.demand
        elif priced is not None and prefill != self.current_prefill:
            # An explicit decode envelope remains a floor, while the candidate
            # role mix supplies the phase costs and measured-domain verdict.
            demand = replace(
                demand,
                prefill_engines=priced.prefill_engines,
                decode_engines=max(priced.decode_engines, demand.decode_engines),
                complete=priced.complete and demand.complete,
                extrapolated=priced.extrapolated,
            )
        if prefill <= 0 or decode <= 0:
            return SplitScore(prefill, decode, float("inf"), float("inf"), float("inf"))
        ttft_ratio = demand.prefill_engines / (prefill * self.utilization)
        if include_resident:
            ttft_ratio = max(
                ttft_ratio,
                self.prefill_pressure * self.current_prefill / prefill,
                self.resident_prefill_s / (prefill * self.ttft_slo),
            )
        tpot_ratio = demand.decode_engines / (decode * self.utilization)
        # Only current residents form the simultaneous decode batch. Resident decode
        # requests stay on their engine after a role change.
        resident_engines = max(decode, self.active_decode_instances or self.current_decode)
        tokens = self.resident_decode_tokens / resident_engines
        requests = self.resident_decode_requests / resident_engines
        # A fractional per-engine average checks one request on each busy engine.
        active_requests = 1.0 if 0 < requests < 1 else requests
        active_tokens = tokens / requests if 0 < requests < 1 else tokens
        profiles = next(
            (
                rows
                for candidate_prefill, rows in self.profile_options
                if candidate_prefill == prefill
            ),
            self.profiles,
        )
        correction = self.decode_correction
        pending_covered = bool(profiles)
        if include_resident:
            for input_len, output_len in self.pending_decode_shapes:
                context = input_len + output_len / 2.0
                if not all(
                    p.decode_rps(
                        self.tpot_slo,
                        context,
                        output_len,
                        correction=correction,
                        request_cap=self.decode_concurrency,
                    )
                    and p.covers_decode(1, context)
                    for p in profiles
                ):
                    pending_covered = False
        capacity = (
            sum(p.max_tokens(self.tpot_slo / correction, active_requests) for p in profiles)
            / len(profiles)
            if profiles
            else None
        )
        kv = [p.kv_capacity_tokens for p in profiles]
        kv_capacity = (
            float(min(value for value in kv if value is not None))
            if kv and all(value is not None for value in kv)
            else None
        )
        request_limit = (
            min(
                p.decode_request_limit(tokens / requests, self.decode_concurrency) for p in profiles
            )
            if requests > 0 and profiles
            else None
        )
        covered = not include_resident or (
            bool(profiles)
            and pending_covered
            and all(
                profile.covers_output(length)
                for profile in profiles
                for length in self.offered_outputs
            )
            and all(p.covers_decode(active_requests, active_tokens) for p in profiles)
        )
        if include_resident and prefill > self.current_prefill:
            covered = covered and self.resident_profile_covered
        if not covered:
            tpot_ratio = float("inf")
        decode_queue_ratio = (
            next((ratio for engines, ratio in self.decode_wait_ratios if engines == decode), 0.0)
            if include_resident
            else 0.0
        )
        if include_resident:
            tpot_ratio = max(
                tpot_ratio,
                self.decode_pressure * self.current_decode / decode,
                self._decode_slots(decode),
            )
            if profiles:
                interval = (
                    sum(
                        p.token_interval(math.ceil(active_tokens), active_requests)
                        for p in profiles
                    )
                    / len(profiles)
                    * correction
                )
                floor = sum(p.token_interval(0) for p in profiles) / len(profiles) * correction
                modelled = (
                    interval / self.tpot_slo
                    if floor >= self.tpot_slo
                    else max(0.0, interval - floor) / (self.tpot_slo - floor)
                )
                tpot_ratio = max(tpot_ratio, modelled)
        return SplitScore(
            prefill=prefill,
            decode=decode,
            ttft_ratio=ttft_ratio,
            tpot_ratio=tpot_ratio,
            objective=max(ttft_ratio, tpot_ratio, decode_queue_ratio),
            decode_queue_ratio=decode_queue_ratio,
            decode_tokens_per_engine=tokens,
            decode_slo_capacity_tokens=(
                capacity if capacity is not None and math.isfinite(capacity) else None
            ),
            decode_kv_capacity_tokens=kv_capacity,
            decode_request_limit=request_limit,
            decode_requests_per_engine=requests,
            pending_decode_requests=self.pending_decode_requests,
            pending_decode_tokens=self.pending_decode_tokens,
            decode_profile_covered=covered,
            decode_correction=correction,
        )


class SplitScorer:
    """Capture fleet work once before evaluating adjacent splits."""

    def __init__(self, demand: DemandModel) -> None:
        self.demand = demand
        self.monitor = demand.monitor
        self.scheduler = demand.scheduler

    def recheck(self, waiting: Iterable[Request]) -> None:
        """Refresh the cache evidence of requests waiting for prefill placement."""
        for row in waiting:
            self.scheduler.recheck_evidence(row, CACHE_RECHECK_S)

    def decode_wait_ratios(self, decodes: Iterable[int]) -> tuple[tuple[int, float], ...]:
        """Return each decode engine count's largest slot wait over the remaining TTFT budget.

        Waiting decode requests and requests in prefill take the pool's slots in handoff order,
        as decode admission projects them. A request past its TTFT deadline at handoff adds
        nothing.
        """
        queued = [r for inst in self.monitor.instances.values() for r in inst.prefill.values()]
        occupancy = self.scheduler.decode_occupancy(
            round(sum(r.input_len for r in queued) / len(queued)) if queued else 0,
            concurrency=self.scheduler.decode_concurrency,
            expected_output=self.demand.output_estimator(),
        )
        if occupancy is None:
            return ()
        rows = []
        for decode in decodes:
            started, _ = occupancy.schedule(occupancy.slots * decode // len(occupancy.engines))
            ratio = max(
                (
                    (start - ready) / (deadline - ready)
                    for (ready, _, _), (start, _, _), deadline in zip(
                        occupancy.queued, started, occupancy.deadlines, strict=True
                    )
                    if start > ready and deadline > ready
                ),
                default=0.0,
            )
            rows.append((decode, ratio))
        return tuple(rows)

    def project_prefill(
        self,
        now: float,
        request: Request | None = None,
        *,
        additional_prefill: tuple[Instance, ...] = (),
    ) -> PrefillProjection | None:
        """Project FIFO prefill completion across the current live prefill pool.

        A supplied `request` joins the projection when absent from `monitor.waiting`.
        """
        pool = [*self.scheduler.live_instances(Role.PREFILL), *additional_prefill]
        if not pool:
            return None
        profiles: dict[str, Profile] = {}
        for inst in pool:
            profile = self.scheduler.profiles.get(inst.iid)
            if profile is None:
                return None
            profiles[inst.iid] = profile

        waiting = [row for row in self.monitor.waiting.values() if row.phase is Phase.PREFILL]
        if request is not None and all(row.rid != request.rid for row in waiting):
            waiting.append(request)
        self.recheck(waiting)
        if not waiting:
            return None
        # Stable sorting preserves queue publication order when several offers
        # share one clock tick.
        waiting.sort(key=lambda row: row.arrived_at if row.arrived_at is not None else now)

        loads: dict[str, float] = {}
        resident_prefill = 0.0
        for inst in pool:
            profile = profiles[inst.iid]
            resident = resident_prefill_seconds(profile, inst)
            penalty = (
                self.scheduler.health.penalty_s
                if self.scheduler.health is not None
                and inst.iid in self.scheduler.health.probation_set()
                else 0.0
            )
            loads[inst.iid] = resident + penalty
            resident_prefill += resident

        focus = request.rid if request is not None else None
        projected: list[tuple[float, Request]] = []
        queued_prefill = 0.0
        for row in waiting:
            choices = []
            for inst in pool:
                profile = profiles[inst.iid]
                work = prefill_seconds(profile, row)
                choices.append((loads[inst.iid] + work, inst.iid, work))
            completion, iid, work = min(choices)
            loads[iid] = completion
            queued_prefill += work
            elapsed = max(0.0, now - (row.arrived_at if row.arrived_at is not None else now))
            projected.append((elapsed + completion, row))

        if focus is not None:
            selected = next((entry for entry in projected if entry[1].rid == focus), None)
            if selected is None:
                return None
        else:
            selected = max(projected, key=lambda entry: (entry[0], entry[1].rid))
        ttft_s, row = selected
        return PrefillProjection(
            rid=row.rid,
            at=now,
            projected_ttft_s=ttft_s,
            ttft_slo_s=self.scheduler.slo.ttft_s,
            resident_prefill_s=resident_prefill,
            queued_prefill_s=queued_prefill,
            waiting_prefill=len(waiting),
        )

    def capture(
        self,
        now: float,
        demand: Demand,
        *,
        utilization: float,
        observed_load: tuple[float, float],
        estimates: OutputEstimates | None = None,
        correction: float | None = None,
        window_s: float | None = None,
        step_s: float | None = None,
        resident_s: float | None = None,
        prefills: Collection[int] | None = None,
    ) -> SplitSnapshot:
        """Resolve live request and profile inputs into immutable values.

        Splits cover the live engines; unavailable engines add no capacity.
        `prefills` limits role-mix pricing to those splits.
        """
        instances = tuple(self.scheduler.live_instances())
        profiles = tuple(
            profile
            for inst in instances
            if (profile := self.scheduler.profiles.get(inst.iid)) is not None
        )
        prefill_profiles = tuple(
            profile
            for inst in self.scheduler.live_instances(Role.PREFILL)
            if (profile := self.scheduler.profiles.get(inst.iid)) is not None
        )
        waiting = tuple(self.monitor.waiting.values())
        resident_prefill = 0.0
        resident_covered = True
        for inst in instances:
            profile = self.scheduler.profiles.get(inst.iid)
            if profile is not None:
                resident_prefill += resident_prefill_seconds(profile, inst)
            if inst.decode:
                resident_covered = (
                    resident_covered
                    and profile is not None
                    and (profile.covers_decode(len(inst.decode), inst.decode_tokens()))
                )
        queued_prefill = 0.0
        if prefill_profiles:
            queued_prefill = sum(
                min(prefill_seconds(p, r) for p in prefill_profiles)
                for r in waiting
                if r.phase is Phase.PREFILL
            )
            resident_prefill += queued_prefill
        pending = [r for inst in instances for r in inst.prefill.values()] + list(waiting)
        estimates = self.demand._output_estimates() if estimates is None else estimates
        pending_shapes = tuple(
            (r.input_len, self.demand._expected_output(r.input_len, r.wanted_len, estimates))
            for r in pending
        )
        current_prefill = sum(inst.role is Role.PREFILL for inst in instances)
        profile_options_list: list[tuple[int, tuple[Profile, ...]]] = []
        # Engines that run prefill under each split.
        prefill_iids: dict[int, set[str]] = {}
        for prefill in range(1, len(instances)):
            if prefills is not None and prefill not in prefills:
                continue
            roles = {inst.iid: inst.role for inst in instances}
            rows: tuple[Profile, ...]
            if abs(prefill - current_prefill) > 1:
                rows = ()
            else:
                if prefill != current_prefill:
                    target = Role.PREFILL if prefill > current_prefill else Role.DECODE
                    donor, _ = self.scheduler.planned_donor(target)
                    if donor is not None:
                        roles[donor.iid] = target
                rows = self.scheduler.profiles.profiles_for_split(
                    roles, prefill, len(instances) - prefill, roles
                )
            profile_options_list.append((prefill, rows))
            prefill_iids[prefill] = {iid for iid, role in roles.items() if role is Role.PREFILL}
        profile_options = tuple(profile_options_list)
        demand_options = (
            tuple(
                (
                    prefill,
                    self.demand.price_profiles(
                        now,
                        window_s=window_s,
                        step_s=step_s,
                        profiles=rows,
                        estimates=estimates,
                        correction=correction,
                        prefill_iids=prefill_iids[prefill],
                        resident_s=resident_s,
                    ),
                )
                for prefill, rows in profile_options
            )
            if window_s is not None and step_s is not None
            else ()
        )
        return SplitSnapshot(
            at=now,
            demand=demand,
            current_prefill=sum(inst.role is Role.PREFILL for inst in instances),
            current_decode=sum(inst.role is Role.DECODE for inst in instances),
            profiles=profiles,
            profiles_complete=len(profiles) == len(instances),
            ttft_slo=self.scheduler.slo.ttft_s,
            tpot_slo=self.scheduler.slo.tpot_s,
            utilization=utilization,
            prefill_pressure=observed_load[0],
            decode_pressure=observed_load[1],
            resident_prefill_s=resident_prefill,
            resident_decode_tokens=sum(i.decode_tokens() for i in instances),
            resident_decode_requests=sum(len(i.decode) for i in instances),
            decode_concurrency=self.scheduler.decode_concurrency,
            waiting_decode_requests=sum(r.phase is Phase.DECODE for r in waiting),
            decode_role_requests=sum(len(i.decode) for i in instances if i.role is Role.DECODE),
            decode_wait_ratios=self.decode_wait_ratios(
                len(instances) - prefill for prefill, _ in profile_options
            ),
            pending_decode_tokens=sum(
                input_len + output / 2.0 for input_len, output in pending_shapes
            ),
            pending_decode_requests=len(pending),
            pending_decode_shapes=pending_shapes,
            decode_correction=(
                self.demand._decode_correction() if correction is None else correction
            ),
            resident_profile_covered=resident_covered,
            queued_prefill_s=queued_prefill,
            active_decode_instances=sum(bool(inst.decode) for inst in instances),
            profile_options=profile_options,
            demand_options=demand_options,
            offered_outputs=tuple(
                (
                    row.value[1]
                    if row.overflow
                    else self.demand._expected_output(row.value[0], row.value[1], estimates)
                )
                for row in self.demand.expected_decode.rows(
                    now - (window_s if window_s is not None else 0.0)
                )
                if row.value[1] > 0
            ),
        )


def decision_details(
    current: SplitScore,
    candidate: SplitScore,
    demand: Demand,
) -> dict[str, object]:
    """Serialize the current and candidate split projections."""
    return {
        "current_prefill": current.prefill,
        "current_decode": current.decode,
        "decision_basis": "demand_projection"
        if demand.complete
        else (
            "prefill_pressure_recovery"
            if candidate.prefill > current.prefill
            else "decode_pressure_recovery"
        ),
        "prefill_work": rounded(demand.prefill_engines),
        "decode_work": rounded(demand.decode_engines),
        "projected_ttft_ratio": rounded(candidate.ttft_ratio),
        "projected_tpot_ratio": rounded(candidate.tpot_ratio),
        "projected_decode_wait_ratio": rounded(candidate.decode_queue_ratio),
        "objective": rounded(candidate.objective),
        "objective_delta": rounded(current.objective - candidate.objective),
        "decode_tokens_per_engine": rounded(candidate.decode_tokens_per_engine, 3),
        "decode_slo_capacity_tokens": (
            round(candidate.decode_slo_capacity_tokens, 3)
            if candidate.decode_slo_capacity_tokens is not None
            else None
        ),
        "decode_kv_capacity_tokens": candidate.decode_kv_capacity_tokens,
        "decode_request_limit": candidate.decode_request_limit,
        "decode_requests_per_engine": rounded(
            candidate.decode_requests_per_engine,
            3,
        ),
        "pending_decode_requests": candidate.pending_decode_requests,
        "pending_decode_tokens": rounded(candidate.pending_decode_tokens, 3),
        "decode_profile_covered": candidate.decode_profile_covered,
        "decode_correction": rounded(candidate.decode_correction),
        "arrivals": demand.arrivals,
        "output_observations": demand.output_observations,
        "demand_complete": demand.complete,
        "extrapolated_arrivals": demand.extrapolated,
        "unsized_offers": demand.unsized,
    }
