"""SLO-aware request placement, pricing, availability and engine health."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Collection, Iterable
from typing import Any

from ...profiling.store import ProfileStore
from ...types import LEG_CONNECTION, Instance, Phase, Request, Role
from .. import costs
from ..availability import EngineAvailability
from ..control import SLO, PrefillFloor, Thresholds
from ..costs import Cost
from ..health import DriftTracker
from ..monitor import InstanceMonitor
from ..outcomes import OutcomeWindow
from ..prefill import prefill_seconds, resident_prefill_seconds
from .roles import RoleChanges

log = logging.getLogger("narwhal.scheduler")


class GlobalScheduler:
    """Request dispatch and instance flipping over a stateless pool."""

    def __init__(
        self,
        monitor: InstanceMonitor,
        profiles: ProfileStore,
        slo: SLO,
        thresholds: Thresholds | None = None,
        clock: Callable[[], float] = time.monotonic,
        eject_after: int = 3,
        flip_history: int = 1000,
        health: DriftTracker | None = None,
        pinned: frozenset[str] = frozenset(),
        min_prefill: int = 1,
        min_decode: int = 1,
        advisory: bool = False,
        on_floor_event: Callable[[dict], None] | None = None,
        on_control_event: Callable[[dict], None] | None = None,
        on_availability_event: Callable[[dict], None] | None = None,
        outcome_bucket_s: float = 1.0,
        outcome_retained_s: float = 480.0,
        decode_concurrency: int = 0,
    ) -> None:
        self.monitor = monitor
        self.decode_concurrency = decode_concurrency
        self.profiles = profiles
        self.slo = slo
        self.th = thresholds or Thresholds()
        self._clock = clock
        self.health = health
        self.min_prefill = max(1, min_prefill)
        self.min_decode = max(1, min_decode)
        self.advisory = advisory
        self.on_floor_event = on_floor_event
        self.on_control_event = on_control_event
        self.on_eject: Callable[[str], None] | None = None
        # Refreshes a request's cache evidence older than the given seconds from current residency.
        self.recheck_cache_evidence: Callable[[Request, float], None] | None = None
        self.roles = RoleChanges(self, flip_history)
        # Placements with no SLO-eligible candidate; refused role changes count separately.
        self.unserved = 0
        self.outcomes = OutcomeWindow(clock, outcome_bucket_s, outcome_retained_s)
        self.availability = EngineAvailability(
            monitor,
            clock,
            eject_after=eject_after,
            on_change=self.refresh_floor_state,
            on_eject=self._notify_eject,
            pinned=pinned,
            on_event=on_availability_event,
        )
        self.prefill_floor = PrefillFloor(
            clock,
            self.prefill_live,
            self.quarantine_list,
            self.ejected,
            self.min_prefill,
            on_floor_event,
        )

    def _notify_eject(self, iid: str) -> None:
        if self.on_eject is not None:
            self.on_eject(iid)

    @property
    def pinned(self) -> frozenset[str]:
        """Engines that take only their configured role's legs."""
        return self.availability.pinned

    @pinned.setter
    def pinned(self, pinned: frozenset[str]) -> None:
        self.availability.pinned = pinned

    @property
    def ejected(self) -> dict[str, float]:
        """Ejected endpoints and their latest probe timestamps."""
        return self.availability.ejected

    @property
    def draining(self) -> set[str]:
        """Operator drains that remain held out across recovery probes."""
        return self.availability.draining

    @property
    def quarantined(self) -> dict[str, float]:
        """Temporary request-path hold-outs keyed by expiry."""
        return self.availability.quarantined

    @property
    def liveness_misses(self) -> dict[str, int]:
        """Consecutive liveness-sweep misses for each endpoint."""
        return self.availability.liveness_misses

    @property
    def verifying(self) -> set[tuple[str, str]]:
        """Endpoint and evidence-kind pairs with a probe in flight."""
        return self.availability.verifying

    @property
    def inference_suspects(self) -> set[str]:
        """Endpoints awaiting inference evidence before readmission."""
        return self.availability.inference_suspects

    def record_failure(self, iid: str, klass: str = LEG_CONNECTION) -> str | None:
        """Record a failed leg and apply its scoped breaker verdict."""
        return self.availability.record_failure(iid, klass)

    def quarantine(self, iid: str, seconds: float) -> bool:
        """Hold a failed endpoint out while preserving one aggregate candidate."""
        return self.availability.quarantine(iid, seconds)

    def quarantine_list(self) -> list[str]:
        """Return the quarantine after expiring elapsed hold-outs."""
        return self.availability.quarantine_list()

    def eject(self, iid: str, cause: str = "unspecified") -> bool:
        """Eject an endpoint for `cause` and update live floors."""
        return self.availability.eject(iid, cause)

    def release_hold(self, iid: str, cause: str) -> bool:
        """Return a held endpoint to placement, recording why its hold ended."""
        return self.availability.release_hold(iid, cause)

    def holds_snapshot(self) -> dict[str, list[dict[str, Any]]]:
        """Report timed quarantines and inference holds separately."""
        return self.availability.holds_snapshot()

    def drain(self, iid: str) -> None:
        """Remove a configured endpoint from new request placements."""
        self.availability.drain(iid)

    def finish_drain(self, iid: str) -> None:
        """Readmit an endpoint whose health and generation were validated."""
        self.availability.finish_drain(iid)

    def record_answer(self, iid: str, evidence: str, *, via: str | None = None) -> None:
        """Clear only the breaker classes established by the answer evidence."""
        self.availability.record_answer(iid, evidence, via=via)

    def breaker_snapshot(self) -> dict[str, Any]:
        """Report every breaker class and pending verification probe."""
        return self.availability.breaker_snapshot()

    def sweep_due(self, after_s: float) -> bool:
        """Advance the liveness sweep timestamp when its interval expires."""
        return self.availability.sweep_due(after_s)

    def probe_due(self, after_s: float) -> list[str]:
        """Return ejected endpoints whose recovery probe is due."""
        return self.availability.probe_due(after_s)

    def live_instances(
        self, role: Role | None = None, *, exclude: set[str] | frozenset[str] = frozenset()
    ) -> list[Instance]:
        """Return endpoints eligible for new work and role changes."""
        return self.availability.live_instances(role, exclude=exclude)

    def role_pool(self, role: Role, instances: list[Instance]) -> list[Instance]:
        """Return the engines in `instances` that place `role`'s legs."""
        return self.availability.role_pool(role, instances)

    def role_covered_without(self, iid: str) -> bool:
        """Return whether another live engine places every role that `iid` places."""
        return self.availability.role_covered_without(iid)

    def cost(
        self,
        request: Request,
        inst: Instance,
        *,
        warm: bool = True,
        probation: Collection[str] | None = None,
    ) -> Cost:
        """Price a request with the current health and prefix-reuse evidence.

        `probation` is the health tracker's probation set, read from it when None.
        """
        return costs.cost(
            request,
            inst,
            monitor=self.monitor,
            profiles=self.profiles,
            slo=self.slo,
            health=self.health,
            warm=warm,
            probation=probation,
        )

    def decode_price(
        self,
        request: Request,
        iid: str,
        requests: int,
        tokens: int,
        *,
        probation: Collection[str] | None = None,
    ) -> float:
        """Price a decode leg on `iid` beside `requests` residents holding `tokens` tokens."""
        return costs.decode_price(
            request,
            iid,
            requests,
            tokens,
            monitor=self.monitor,
            profiles=self.profiles,
            slo=self.slo,
            health=self.health,
            probation=probation,
        )

    def meets_slo(self, request: Request, cost: Cost, *, ttft_margin: float = 0.0) -> bool:
        """Compare the placement price with its phase latency budget."""
        return costs.meets_slo(request, cost, slo=self.slo, ttft_margin=ttft_margin)

    def prefill_load(self, inst: Instance) -> float:
        """Return prefill work as a ratio to the TTFT target."""
        return costs.prefill_load(inst, monitor=self.monitor, profiles=self.profiles, slo=self.slo)

    def decode_load(self, inst: Instance) -> float:
        """Return observed decode latency above the profiled idle floor."""
        return costs.decode_load(inst, monitor=self.monitor, profiles=self.profiles, slo=self.slo)

    @property
    def outcome_bucket_s(self) -> float:
        """Width of one completed-outcome evidence bucket."""
        return self.outcomes.bucket_s

    @property
    def outcome_retained_s(self) -> float:
        """Duration retained for SLO outcome diagnostics."""
        return self.outcomes.retained_s

    def note_outcome(self, ttft_ok: bool, tpot_ok: bool, *, at: float | None = None) -> None:
        """Record one completed request in the attainment evidence window."""
        self.outcomes.note_outcome(ttft_ok, tpot_ok, at=at)

    def outcome_counts(self, since_s: float) -> tuple[int, int, int]:
        """Count completed SLO outcomes since the requested time."""
        return self.outcomes.outcome_counts(since_s)

    def outcome_summary(self) -> dict[str, int | float]:
        """Report retained attainment evidence and pruning counts."""
        return self.outcomes.outcome_summary()

    def refresh_floor_state(self) -> None:
        """Update prefill-floor breach edges using current availability."""
        self.prefill_floor.minimum = self.min_prefill
        self.prefill_floor.on_event = self.on_floor_event
        self.prefill_floor.refresh()

    def floor_snapshot(self) -> dict:
        """Report the live prefill-floor breach and elapsed duration."""
        return self.prefill_floor.snapshot()

    def prefill_live(self) -> int:
        """Live prefill count used by the floor guard."""
        return len(self.live_instances(Role.PREFILL))

    def decode_live(self) -> int:
        """Live decode count used by the floor guard."""
        return len(self.live_instances(Role.DECODE))

    def _prefill_candidates(self) -> list[Instance]:
        """Return live prefill candidates, falling back to all live engines."""
        live = self.live_instances()
        return [i for i in live if i.role is Role.PREFILL] or live

    def prefill_admission_price(self, request: Request, inst: Instance) -> float:
        """Return the prefill price for predictive admission.

        Without live prefill engines, a busy decode engine prices at infinity.
        """
        if not self.live_instances(Role.PREFILL) and inst.role is Role.DECODE and inst.decode:
            return float("inf")
        return self.cost(request, inst)[1]

    def role_placeable(self, role: Role) -> bool:
        """Return whether a live engine of `role`, or a live unpinned engine, can take a leg."""
        return bool(self.role_pool(role, self.live_instances()))

    def prefill_ready_s(self, request: Request, inst: Instance) -> float:
        """Return seconds until `request` finishes prefill behind `inst`'s resident queue."""
        profile = self.profiles.get(inst.iid)
        if profile is None:
            return 0.0
        return resident_prefill_seconds(profile, inst) + prefill_seconds(profile, request)

    def cheapest_own_prefill(self, request: Request) -> float | None:
        """Return the request's cheapest isolated prefill cost.

        Resident queues and probation penalties are excluded.
        """
        floors = [
            prefill_seconds(profile, request)
            for inst in self._prefill_candidates()
            if (profile := self.profiles.get(inst.iid)) is not None
        ]
        return min(floors) if floors else None

    def pool_load(self, role: Role) -> float:
        """Return mean load over engines currently doing a phase's work.

        Resident work remains in phase feedback after an engine changes role.
        """
        doing = [
            instance
            for instance in self.live_instances()
            if instance.role is role
            or (instance.prefill if role is Role.PREFILL else instance.decode)
        ]
        phase_load = self.prefill_load if role is Role.PREFILL else self.decode_load
        return sum(phase_load(instance) for instance in doing) / len(doing) if doing else 0.0

    def decode_exclusion(self, request: Request, exclude: Collection[str] = ()) -> set[str]:
        """Return the request's producer when another engine can take its decode leg.

        A decode leg on its own producer reuses that engine's prefix cache and never pulls
        the KV handoff, so the producer holds the blocks until its lease expires. The
        producer takes the decode leg only when the decode pool holds no other engine.
        """
        producer = request.prefill_instance
        if request.phase is not Phase.DECODE or producer is None:
            return set()
        pool = self.role_pool(Role.DECODE, self.live_instances(exclude=set(exclude)))
        return {producer} if any(inst.iid != producer for inst in pool) else set()

    def schedule(self, request: Request, exclude: set[str] | None = None) -> Instance:
        """Place one phase without changing roles, excluding failed endpoints."""
        exclude = set(exclude or ())
        exclude |= self.decode_exclusion(request, exclude)
        instances = self.live_instances(exclude=exclude)
        if not instances:
            raise RuntimeError("no schedulable instances")

        # Profiles model sequential prefill and batched decode.
        want = Role.PREFILL if request.phase is Phase.PREFILL else Role.DECODE
        candidates = self.role_pool(want, instances)
        if not candidates:
            raise RuntimeError("no schedulable instances for the pinned roles")
        if request.phase is Phase.PREFILL:
            request.cache_placement = None
            self.recheck_evidence(request, 0.0)
        probation = self.health.probation_set() if self.health is not None else None
        prices = {i.iid: self.cost(request, i, probation=probation) for i in candidates}

        chosen, served = self._cheapest(request, candidates, prices)
        if not served:
            # Admission decides over-budget placements.
            self.unserved += 1
        if request.phase is Phase.PREFILL and request.cached_tokens:
            request.cache_placement = self._cache_placement(
                request, chosen, candidates, prices, probation
            )
        return chosen

    def recheck_evidence(self, request: Request, fresh_s: float) -> None:
        """Refresh a prefill request's cache evidence older than `fresh_s` seconds."""
        if (
            request.phase is Phase.PREFILL
            and request.cached_tokens
            and self.recheck_cache_evidence is not None
        ):
            self.recheck_cache_evidence(request, fresh_s)

    def _cheapest(
        self, request: Request, candidates: list[Instance], prices: dict[str, Cost]
    ) -> tuple[Instance, bool]:
        """Return the cheapest candidate within the SLO, else the cheapest, and whether it fits."""
        eligible = [i for i in candidates if self.meets_slo(request, prices[i.iid])]
        return min(eligible or candidates, key=lambda i: (prices[i.iid], i.iid)), bool(eligible)

    def _cache_placement(
        self,
        request: Request,
        chosen: Instance,
        candidates: list[Instance],
        prices: dict[str, Cost],
        probation: Collection[str] | None,
    ) -> dict[str, Any] | None:
        """Return cache-evidence pricing details for the chosen prefill engine."""
        profile = self.profiles.get(chosen.iid)
        if profile is None:
            return None
        return {
            "placed_iid": chosen.iid,
            "placed_cached_tokens": request.cached_tokens.get(chosen.iid, 0),
            "evidence_sequence": request.cache_sequences.get(chosen.iid),
            "predicted_prefill_s": prefill_seconds(profile, request),
            "cold_prefill_s": profile.prefill_time(request.input_len),
            "cold_choice_iid": self._cold_choice(request, candidates, prices, probation),
        }

    def _cold_choice(
        self,
        request: Request,
        candidates: list[Instance],
        prices: dict[str, Cost],
        probation: Collection[str] | None,
    ) -> str:
        # Without positive cache evidence on an engine, its warm price is its cold price.
        cold = {
            i.iid: (
                self.cost(request, i, warm=False, probation=probation)
                if _cached_on(i.iid, request, i.prefill.values())
                else prices[i.iid]
            )
            for i in candidates
        }
        return self._cheapest(request, candidates, cold)[0].iid

    def health_pass(self) -> None:
        """Sample live engines and apply drift verdicts."""
        if self.health is None:
            return
        for inst in self.monitor.instances.values():
            if inst.iid in self.ejected:
                continue
            if not self.monitor.decode_profile_eligible(inst.iid):
                self.health.pause_for_prefill(inst.iid)
                continue
            observed = max(
                self.monitor.current_token_interval(inst.iid),
                self.monitor.stalled_gap(inst.iid),
            )
            profile = self.profiles.get(inst.iid)
            if profile is None:
                continue
            # Compare with the profiled interval at the current batch size.
            expected = profile.token_interval(inst.decode_tokens(), len(inst.decode))
            if observed > 0.0 and expected > 0.0:
                self.health.note(inst.iid, observed / expected)
        for verdict, iid in self.health.tick():
            if verdict == "evict" and self.role_covered_without(iid) and self.eject(iid, "drift"):
                self.health.evicted(iid)
                log.warning(
                    "ejected %s after sustained drift",
                    iid,
                )
            elif verdict == "evict":
                log.warning(
                    "health: %s drifts past the band but alone serves its role; "
                    "probation stands, eviction refused",
                    iid,
                )


def _cached_on(iid: str, request: Request, residents: Iterable[Request]) -> bool:
    """Return whether `request` or a resident holds positive cache evidence for `iid`."""
    return request.cached_tokens.get(iid, 0) > 0 or any(
        r.cached_tokens.get(iid, 0) > 0 for r in residents
    )
