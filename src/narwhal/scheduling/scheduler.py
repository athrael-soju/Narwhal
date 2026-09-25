"""SLO-aware request placement, role changes, and pool-load control."""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Callable
from typing import Any

from ..profiling.store import ProfileStore
from ..types import LEG_CONNECTION, Instance, Phase, Request, Role
from . import costs
from .availability import EngineAvailability
from .control import SLO, Flip, PrefillFloor, Thresholds
from .costs import Cost
from .health import DriftTracker
from .monitor import InstanceMonitor
from .outcomes import OutcomeWindow

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
        outcome_bucket_s: float = 1.0,
        outcome_retained_s: float = 480.0,
    ) -> None:
        self.monitor = monitor
        self.profiles = profiles
        self.slo = slo
        self.th = thresholds or Thresholds()
        self._clock = clock
        self.health = health
        self.pinned = pinned
        self.min_prefill = max(1, min_prefill)
        self.min_decode = max(1, min_decode)
        self.advisory = advisory
        self.on_floor_event = on_floor_event
        self.on_control_event = on_control_event
        self.on_eject: Callable[[str], None] | None = None
        # Apply cooldown to the opening P-to-D change as well.
        self._last_p2d_flip = clock()
        self.panic_bypasses = 0
        self._panic_sustained = 0
        self._last_flip: dict[str, float] = {}
        # Bound telemetry retained for `/narwhal/state`.
        self._flip_history = flip_history
        self.flips: list[Flip] = []
        # Distinguish unavailable SLO placements from refused role changes.
        self.unserved = 0
        self.flips_refused: list[tuple[float, str, str]] = []
        # Process-lifetime counters must outlive the bounded diagnostic histories.
        self._flip_counts: Counter[tuple[str, str]] = Counter()
        self._flip_reversals = 0
        self._flip_refusals = 0
        self._flip_inflight: Counter[str] = Counter()
        self._last_flip_role: dict[str, Role] = {}
        self._decode_floor_restores = 0
        self._last_decision: dict | None = None
        self._decision_counts: Counter[tuple[str, str]] = Counter()
        self.outcomes = OutcomeWindow(clock, outcome_bucket_s, outcome_retained_s)
        self.availability = EngineAvailability(
            monitor,
            clock,
            eject_after=eject_after,
            on_change=self.refresh_floor_state,
            on_eject=self._notify_eject,
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

    def eject(self, iid: str) -> bool:
        """Eject an endpoint after failed verification and update live floors."""
        return self.availability.eject(iid)

    def drain(self, iid: str) -> None:
        """Remove a configured endpoint from new request placements."""
        self.availability.drain(iid)

    def finish_drain(self, iid: str) -> None:
        """Readmit an endpoint whose health and generation were validated."""
        self.availability.finish_drain(iid)

    def record_answer(self, iid: str, evidence: str) -> None:
        """Clear only the breaker classes established by the answer evidence."""
        self.availability.record_answer(iid, evidence)

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

    def cost(self, request: Request, inst: Instance) -> Cost:
        """Price a request with the current health and prefix-reuse evidence."""
        return costs.cost(
            request,
            inst,
            monitor=self.monitor,
            profiles=self.profiles,
            slo=self.slo,
            health=self.health,
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

    def decode_floor_snapshot(self) -> dict:
        """Report live decode capacity and floor recovery."""
        live_decode = self.decode_live()
        return {
            "min_decode": self.min_decode,
            "live_decode": live_decode,
            "below_floor": live_decode < self.min_decode,
            "restoration_moves": self._decode_floor_restores,
        }

    def control_snapshot(self) -> dict:
        """Return advisory mode and the most recent controller decision."""
        return {
            "advisory": self.advisory,
            "last_decision": dict(self._last_decision) if self._last_decision else None,
            "decisions": {
                f"{by}:{result}": count
                for (by, result), count in sorted(self._decision_counts.items())
            },
            "flips": {f"{by}:{to}": n for (by, to), n in sorted(self._flip_counts.items())},
            "flip_reversals": self._flip_reversals,
            "flips_refused": self._flip_refusals,
            "flip_inflight": {phase.value: self._flip_inflight[phase.value] for phase in Phase},
        }

    def _record_flip_refusal(self, target: Role, reason: str) -> None:
        self._flip_refusals += 1
        self.flips_refused.append((self._clock(), target.value, reason))
        del self.flips_refused[: -self._flip_history]

    def _control_event(self, event: str, **fields: object) -> None:
        if self.on_control_event is not None:
            self.on_control_event({"event": event, "at": self._clock(), **fields})

    def record_decision(
        self,
        *,
        prefill: int,
        decode: int,
        by: str,
        reason: str,
        result: str,
        details: dict[str, object] | None = None,
    ) -> None:
        """Retain one controller decision for state and metric output."""
        self._decision_counts[(by, result)] += 1
        self._last_decision = {
            "at": self._clock(),
            "prefill": prefill,
            "decode": decode,
            "by": by,
            "reason": reason,
            "result": result,
            "applied": result == "applied",
            **(details or {}),
        }
        self._control_event(
            "controller_decision",
            **{key: value for key, value in self._last_decision.items() if key != "at"},
        )

    def allow_decode_scale_down(
        self, by: str, *, decision_details: dict[str, object] | None = None
    ) -> bool:
        """Return whether one live decode engine may move to prefill."""
        if self.decode_live() - 1 >= self.min_decode:
            return True
        reason = f"min_decode keeps {self.min_decode} live decode engines"
        proposed_prefill = len(self.monitor.pool(Role.PREFILL)) + 1
        self.record_decision(
            prefill=proposed_prefill,
            decode=len(self.monitor.instances) - proposed_prefill,
            by=by,
            reason=reason,
            result="blocked",
            details=decision_details,
        )
        self._record_flip_refusal(Role.PREFILL, reason)
        return False

    def _prefill_candidates(self) -> list[Instance]:
        """Return live prefill candidates, falling back to all live engines."""
        live = self.live_instances()
        return [i for i in live if i.role is Role.PREFILL] or live

    def prefill_admission_price(self, request: Request, inst: Instance) -> float:
        """Return the prefill price for predictive admission.

        Aggregate fallback requires an idle decode engine because the profile
        covers isolated prefill and decode. Open admission uses normal placement.
        """
        if not self.live_instances(Role.PREFILL) and inst.role is Role.DECODE and inst.decode:
            return float("inf")
        return self.cost(request, inst)[1]

    def cheapest_own_prefill(self, request: Request) -> float | None:
        """Return the request's cheapest isolated prefill cost.

        This excludes resident queues and probation penalties, which can drain
        while a request waits.
        """
        floors = [
            profile.prefill_time(request.input_len)
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

    def flip_cost(self, inst: Instance) -> Cost:
        """Return the resident-work cost of changing an engine's role.

        Prefill instance: `(I[D = empty], sum T(rp, i))`.
        Decode instance:  `(I[P = empty], sum L(rd))`.

        The indicator is 0 when the other type is still resident, so an
        incompletely flipped instance sorts first under argmin.
        """
        profile = self.profiles.get(inst.iid)
        if inst.role is Role.PREFILL:
            indicator = 0.0 if inst.decode else 1.0
            resident = (
                sum(profile.prefill_time(r.input_len) for r in inst.prefill.values())
                if profile
                else float(inst.prefill_tokens())
            )
            return (indicator, resident)
        indicator = 0.0 if inst.prefill else 1.0
        return (indicator, float(inst.decode_tokens()))

    def planned_donor(
        self,
        target: Role,
        *,
        candidate: Instance | None = None,
        bypass_dwell: bool = False,
    ) -> tuple[Instance | None, str]:
        """Resolve the donor used by a score and the subsequent role change."""
        source = Role.DECODE if target is Role.PREFILL else Role.PREFILL
        pool = [
            inst
            for inst in self.live_instances(source)
            if inst.iid not in self.pinned and (candidate is None or inst is candidate)
        ]
        if not pool:
            return None, "pins or nominated engine exclude every source candidate"
        if self.th.dwell_s > 0.0 and not bypass_dwell:
            now = self._clock()
            pool = [
                inst
                for inst in pool
                if now - self._last_flip.get(inst.iid, float("-inf")) >= self.th.dwell_s
            ]
            if not pool:
                return None, "source engine dwell has not elapsed"
        if any(profile.colocated_group for profile in self.profiles.all_profiles()):
            roles = {iid: inst.role for iid, inst in self.monitor.instances.items()}
            prefill = sum(role is Role.PREFILL for role in roles.values())
            prefill += 1 if target is Role.PREFILL else -1
            pool = [
                inst
                for inst in pool
                if self.profiles.profiles_for_split(
                    roles, prefill, len(roles) - prefill, roles | {inst.iid: target}
                )
            ]
            if not pool:
                return None, "measured shared-device profiles exclude every source candidate"
        return min(pool, key=self.flip_cost), ""

    def flip(
        self,
        target: Role,
        by: str = "?",
        *,
        candidate: Instance | None = None,
        bypass_cooldown: bool = False,
        bypass_dwell: bool = False,
        decision_details: dict[str, object] | None = None,
    ) -> Instance | None:
        """Apply a live role change and record its timing, residents and events.

        Recovery may nominate a candidate and bypass timing guards. Pins,
        availability, role floors and transition bookkeeping still apply.
        """
        take_from = Role.DECODE if target is Role.PREFILL else Role.PREFILL
        now = self._clock()

        def blocked(reason: str) -> None:
            self._record_flip_refusal(target, reason)
            current_prefill = len(self.monitor.pool(Role.PREFILL))
            proposed_prefill = current_prefill + (1 if target is Role.PREFILL else -1)
            self.record_decision(
                prefill=proposed_prefill,
                decode=len(self.monitor.instances) - proposed_prefill,
                by=by,
                reason=reason,
                result="blocked",
                details=decision_details,
            )

        if (
            target is Role.DECODE
            and not bypass_cooldown
            and now - self._last_p2d_flip < self.th.cooldown_s
        ):
            if not self._panic_now():
                blocked("decode cooldown has not elapsed")
                return None
            self.panic_bypasses += 1
            log.info(
                "decode cooldown bypass: load %.2f >= %.1fx expand, "
                "prefill %.2f <= shrink, sustained %d passes",
                self.pool_load(Role.DECODE),
                self.th.panic_ratio,
                self.pool_load(Role.PREFILL),
                self._panic_sustained,
            )

        # Ejected and quarantined engines add no target-pool capacity.
        live = self.live_instances(take_from)
        if len(live) <= 1:
            blocked(f"{take_from.value} floor keeps the last live engine")
            return None
        # Pinned and dwelling engines still count toward the source pool floor.
        if take_from is Role.PREFILL and len(live) - 1 < self.min_prefill:
            blocked(f"min_prefill keeps {self.min_prefill} live prefill engines")
            return None
        if take_from is Role.DECODE and not self.allow_decode_scale_down(
            by, decision_details=decision_details
        ):
            return None
        chosen, donor_block = self.planned_donor(
            target, candidate=candidate, bypass_dwell=bypass_dwell
        )
        if chosen is None:
            blocked(donor_block)
            return None
        if (
            target is Role.PREFILL
            and self.th.flip_resident_guard > 0
            and len(chosen.decode) > self.th.flip_resident_guard
        ):
            blocked(
                f"lightest decode donor carries {len(chosen.decode)} residents, "
                f"above flip_resident_guard {self.th.flip_resident_guard}"
            )
            return None
        current_prefill = len(self.monitor.pool(Role.PREFILL))
        proposed_prefill = current_prefill + (1 if target is Role.PREFILL else -1)
        reason = {
            "decode_floor": "restore min_decode",
            "floor_recovery": "restore min_prefill",
            "reactive": "post-move objective improved",
        }.get(by, by)
        if self.advisory:
            self.record_decision(
                prefill=proposed_prefill,
                decode=len(self.monitor.instances) - proposed_prefill,
                by=by,
                reason=reason,
                result="advisory",
                details=decision_details,
            )
            self._record_flip_refusal(target, "advisory mode")
            return None
        self._flip_counts[(by, target.value)] += 1
        if self._last_flip_role.get(chosen.iid) not in (None, target):
            self._flip_reversals += 1
        self._last_flip_role[chosen.iid] = target
        self._flip_inflight[Phase.PREFILL.value] += len(chosen.prefill)
        self._flip_inflight[Phase.DECODE.value] += len(chosen.decode)
        chosen.role = target
        self._last_flip[chosen.iid] = now
        if target is Role.DECODE:
            self._last_p2d_flip = now
        self.flips.append(
            Flip(
                at=now,
                iid=chosen.iid,
                to=target,
                by=by,
                prefill_inflight=len(chosen.prefill),
                decode_inflight=len(chosen.decode),
                resident_ids=frozenset(chosen.decode if target is Role.PREFILL else chosen.prefill),
            )
        )
        self.record_decision(
            prefill=proposed_prefill,
            decode=len(self.monitor.instances) - proposed_prefill,
            by=by,
            reason=reason,
            result="applied",
            details=decision_details,
        )
        del self.flips[: -self._flip_history]
        log.info(
            "FLIP %s %s -> %s | carrying %dP %dD",
            by,
            chosen.iid,
            target.value,
            len(chosen.prefill),
            len(chosen.decode),
        )
        self.refresh_floor_state()
        return chosen

    def restore_decode_floor(self) -> Instance | None:
        """Restore one missing decode engine without waiting on cooldown."""
        if self.decode_live() >= self.min_decode:
            return None
        moved = self.flip(Role.DECODE, "decode_floor", bypass_cooldown=True)
        if moved is None:
            return None
        self._decode_floor_restores += 1
        self._control_event(
            "decode_floor_restored",
            iid=moved.iid,
            live_decode=self.decode_live(),
            min_decode=self.min_decode,
        )
        return moved

    def settle_drains(self) -> None:
        """Close the drain on any flip whose caught work has finished."""
        now = self._clock()
        for f in self.flips:
            if f.drained_s is not None:
                continue
            inst = self.monitor.instances.get(f.iid)
            if inst is None:
                continue
            stale = inst.decode if f.to is Role.PREFILL else inst.prefill
            if not f.resident_ids.intersection(stale):
                f.drained_s = now - f.at

    def schedule(self, request: Request, exclude: set[str] | None = None) -> Instance:
        """Place one phase without changing roles, excluding failed endpoints."""
        exclude = exclude or set()
        instances = self.live_instances(exclude=exclude)
        if not instances:
            raise RuntimeError("no schedulable instances")

        # 1. Prefill instance already flipped to decode: no KV transfer needed.
        if (
            request.phase is Phase.DECODE
            and request.prefill_instance
            and request.prefill_instance not in exclude
        ):
            prior = next(
                (i for i in instances if i.iid == request.prefill_instance),
                None,
            )
            if prior is not None and prior.role is Role.DECODE:
                return prior

        # Profiles assume sequential prefill and batched decode, so prefer the
        # matching role whenever that pool has a live engine.
        want = Role.PREFILL if request.phase is Phase.PREFILL else Role.DECODE
        candidates = [i for i in instances if i.role is want] or instances
        costs = {i.iid: self.cost(request, i) for i in candidates}

        # 2. Lowest-cost instance that also meets the SLO.
        eligible = [i for i in candidates if self.meets_slo(request, costs[i.iid])]
        if eligible:
            chosen = min(eligible, key=lambda i: (costs[i.iid], i.iid))
            return chosen

        # Admission decides whether to accept an over-budget placement.
        # Role changes belong to the monitoring controller.
        self.unserved += 1
        return min(candidates, key=lambda i: (costs[i.iid], i.iid))

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
            if verdict == "evict" and self.availability._can_hold_out(iid) and self.eject(iid):
                self.health.evicted(iid)
                log.warning(
                    "ejected %s after sustained drift",
                    iid,
                )
            elif verdict == "evict":
                log.warning(
                    "health: %s drifts past the band but is the last instance; "
                    "probation stands, eviction refused",
                    iid,
                )

    def observe_control_load(self, prefill: float, decode: float) -> None:
        """Update the sustained cooldown-bypass condition.

        Decode must exceed the panic threshold while prefill remains below
        `shrink`. This excludes fleet-wide spikes.
        """
        armed = (
            self.th.panic_ratio > 0.0
            and decode >= self.th.panic_ratio * self.th.expand
            and prefill <= self.th.shrink
        )
        self._panic_sustained = self._panic_sustained + 1 if armed else 0

    def _panic_now(self) -> bool:
        """Return whether current loads permit a cooldown bypass.

        Live loads are checked again after the sustained counter matures.
        """
        if self.th.panic_ratio <= 0.0:
            return False
        if self._panic_sustained < self.th.sustained_intervals:
            return False
        return (
            self.pool_load(Role.DECODE) >= self.th.panic_ratio * self.th.expand
            and self.pool_load(Role.PREFILL) <= self.th.shrink
        )
