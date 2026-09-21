"""Project phase pressure and resident work for a proposed role split."""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..profiling.model import Profile
from ..types import Instance, Phase, Request, Role
from .demand import Demand, DemandModel, OutputEstimates, rounded


@dataclass(frozen=True)
class SplitScore:
    """Predicted SLO pressure after assigning a complete live fleet."""

    prefill: int
    decode: int
    ttft_ratio: float
    tpot_ratio: float
    objective: float
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
    decode_correction: float
    resident_profile_covered: bool
    queued_prefill_s: float = 0.0

    @property
    def prefill_recovery_ratio(self) -> float:
        """Include known waiting work when dispatch slots hide prefill pressure."""
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
        demand = demand or self.demand
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
        tokens = self.resident_decode_tokens / decode
        requests = self.resident_decode_requests / decode
        profiles = self.profiles
        correction = self.decode_correction
        capacity = (
            sum(p.max_tokens(self.tpot_slo / correction, requests) for p in profiles)
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
            min(p.decode_request_limit(tokens / requests) for p in profiles)
            if requests > 0 and profiles
            else None
        )
        covered = not include_resident or (
            bool(profiles) and all(p.covers_decode(requests, tokens) for p in profiles)
        )
        if include_resident and prefill > self.current_prefill:
            covered = covered and self.resident_profile_covered
        if not covered:
            tpot_ratio = float("inf")
        if include_resident:
            tpot_ratio = max(tpot_ratio, self.decode_pressure * self.current_decode / decode)
            if profiles:
                interval = (
                    sum(p.token_interval(math.ceil(tokens), requests) for p in profiles)
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
            objective=max(ttft_ratio, tpot_ratio),
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

    def project_prefill(
        self,
        now: float,
        request: Request | None = None,
        *,
        additional_prefill: tuple[Instance, ...] = (),
    ) -> PrefillProjection | None:
        """Project FIFO prefill completion across the current live pool.

        The global admission queue has no engine assignment. List scheduling
        translates its FIFO order into the earliest profiled completion on the
        current prefill pool. A supplied request joins the projection during
        the interval before router publication in `monitor.waiting`.
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
        if not waiting:
            return None
        # Stable sorting preserves queue publication order when several offers
        # share one clock tick.
        waiting.sort(key=lambda row: row.arrived_at if row.arrived_at is not None else now)

        loads: dict[str, float] = {}
        resident_prefill = 0.0
        for inst in pool:
            profile = profiles[inst.iid]
            resident = sum(profile.prefill_time(row.input_len) for row in inst.prefill.values())
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
                work = profile.prefill_time(row.input_len)
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
    ) -> SplitSnapshot:
        """Resolve live request and profile inputs into immutable values."""
        instances = tuple(self.monitor.instances.values())
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
                resident_prefill += sum(
                    profile.prefill_time(r.input_len) for r in inst.prefill.values()
                )
            if inst.decode:
                resident_covered = (
                    resident_covered
                    and profile is not None
                    and (profile.covers_decode(len(inst.decode), inst.decode_tokens()))
                )
        queued_prefill = 0.0
        if prefill_profiles:
            queued_prefill = sum(
                min(p.prefill_time(r.input_len) for p in prefill_profiles)
                for r in waiting
                if r.phase is Phase.PREFILL
            )
            resident_prefill += queued_prefill
        pending = [r for inst in instances for r in inst.prefill.values()] + list(waiting)
        estimates = self.demand._output_estimates() if estimates is None else estimates
        pending_tokens = sum(
            r.input_len + self.demand._expected_output(r.input_len, r.wanted_len, estimates) / 2.0
            for r in pending
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
            resident_decode_tokens=sum(i.decode_tokens() for i in instances) + pending_tokens,
            resident_decode_requests=sum(len(i.decode) for i in instances) + len(pending),
            pending_decode_tokens=pending_tokens,
            pending_decode_requests=len(pending),
            decode_correction=(
                self.demand._decode_correction() if correction is None else correction
            ),
            resident_profile_covered=resident_covered,
            queued_prefill_s=queued_prefill,
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
    }
