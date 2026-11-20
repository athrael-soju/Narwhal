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
from .occupancy import SlotRefusals, SlotWaitHeadroom
from .roles import RoleChanges

log = logging.getLogger("narwhal.scheduler")


class GlobalScheduler:
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
        prefill_on_decode: bool = False,
    ) -> None:
        self.monitor = monitor
        self.profiles = profiles
        self.slo = slo
        self.th = thresholds or Thresholds()
        self._clock = clock
        self.health = health
        self.min_prefill = max(1, min_prefill)
        self.min_decode = max(1, min_decode)
        self.advisory = advisory
        # Whether a decode-role engine joins the prefill candidates inside its envelope.
        self.prefill_on_decode = prefill_on_decode
        # Returns the controller's output estimator; the router sets it.
        self.output_estimator: Callable[[], Callable[[Request], int]] = lambda: lambda r: 0
        self.on_floor_event = on_floor_event
        self.on_control_event = on_control_event
        self.on_eject: Callable[[str], None] | None = None
        # Set when engines change roles on the engine side: a donor must be idle, and the
        # callback applies each change.
        self.switch_requires_idle = False
        self.on_flip: Callable[[Instance, Role], None] | None = None
        # A busy donor held out of new placements until it is idle for an engine-side switch,
        # with when the controller last asked for the switch. The hold lapses after
        # `switch_hold_s` without a request.
        self.switch_holds: dict[str, float] = {}
        self.switch_hold_s = 15.0
        # Whether a decode engine must refresh before it takes a handoff from a producer, by
        # engine ID.
        self.stale_handoff: Callable[[str, str], bool] | None = None
        # Refreshes a request's cache evidence older than the given seconds from current residency.
        self.recheck_cache_evidence: Callable[[Request, float], None] | None = None
        self.roles = RoleChanges(self, flip_history)
        # Placements with no SLO-eligible candidate; refused role changes count separately.
        self.unserved = 0
        self.outcomes = OutcomeWindow(clock, outcome_bucket_s, outcome_retained_s)
        # TTFT budget kept free for a request admitted to wait for a decode slot.
        self.slot_headroom = SlotWaitHeadroom(clock)
        # Requests refused for their decode slot wait, which the role controller counts as
        # decode demand.
        self.slot_refusals = SlotRefusals(clock)
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
        return self.availability.pinned

    @pinned.setter
    def pinned(self, pinned: frozenset[str]) -> None:
        self.availability.pinned = pinned

    @property
    def ejected(self) -> dict[str, float]:
        """Latest probe time by ejected endpoint."""
        return self.availability.ejected

    @property
    def draining(self) -> set[str]:
        return self.availability.draining

    @property
    def quarantined(self) -> dict[str, float]:
        """Hold expiry by endpoint; infinite for an inference hold."""
        return self.availability.quarantined

    @property
    def liveness_misses(self) -> dict[str, int]:
        return self.availability.liveness_misses

    @property
    def verifying(self) -> set[tuple[str, str]]:
        return self.availability.verifying

    @property
    def inference_suspects(self) -> set[str]:
        return self.availability.inference_suspects

    def record_failure(self, iid: str, klass: str = LEG_CONNECTION) -> str | None:
        return self.availability.record_failure(iid, klass)

    def quarantine(self, iid: str, seconds: float) -> bool:
        return self.availability.quarantine(iid, seconds)

    def quarantine_list(self) -> list[str]:
        return self.availability.quarantine_list()

    def eject(self, iid: str, cause: str) -> bool:
        return self.availability.eject(iid, cause)

    def release_hold(self, iid: str, cause: str) -> bool:
        return self.availability.release_hold(iid, cause)

    def holds_snapshot(self) -> dict[str, list[dict[str, Any]]]:
        return self.availability.holds_snapshot()

    def drain(self, iid: str) -> None:
        self.availability.drain(iid)

    def finish_drain(self, iid: str) -> None:
        self.availability.finish_drain(iid)

    def record_answer(self, iid: str, evidence: str, *, via: str | None = None) -> None:
        self.availability.record_answer(iid, evidence, via=via)

    def breaker_snapshot(self) -> dict[str, Any]:
        return self.availability.breaker_snapshot()

    def sweep_due(self, after_s: float) -> bool:
        return self.availability.sweep_due(after_s)

    def probe_due(self, after_s: float) -> list[str]:
        return self.availability.probe_due(after_s)

    def live_instances(
        self, role: Role | None = None, *, exclude: set[str] | frozenset[str] = frozenset()
    ) -> list[Instance]:
        return self.availability.live_instances(role, exclude=exclude)

    def role_pool(self, role: Role, instances: list[Instance]) -> list[Instance]:
        return self.availability.role_pool(role, instances)

    def role_covered_without(self, iid: str) -> bool:
        return self.availability.role_covered_without(iid)

    def cost(
        self,
        request: Request,
        inst: Instance,
        *,
        warm: bool = True,
        probation: Collection[str] | None = None,
    ) -> Cost:
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
        return costs.meets_slo(request, cost, slo=self.slo, ttft_margin=ttft_margin)

    def handoff_s(self) -> float:
        delays = [self.monitor.handoff_delay(inst.iid) for inst in self.monitor.pool(Role.DECODE)]
        return sum(delays) / len(delays) if delays else 0.0

    def prefill_load(self, inst: Instance) -> float:
        return costs.prefill_load(inst, monitor=self.monitor, profiles=self.profiles, slo=self.slo)

    def decode_load(self, inst: Instance) -> float:
        return costs.decode_load(inst, monitor=self.monitor, profiles=self.profiles, slo=self.slo)

    @property
    def outcome_bucket_s(self) -> float:
        return self.outcomes.bucket_s

    @property
    def outcome_retained_s(self) -> float:
        return self.outcomes.retained_s

    def note_outcome(self, ttft_ok: bool, tpot_ok: bool, *, at: float | None = None) -> None:
        self.outcomes.note_outcome(ttft_ok, tpot_ok, at=at)

    def outcome_counts(self, since_s: float) -> tuple[int, int, int]:
        return self.outcomes.outcome_counts(since_s)

    def outcome_summary(self) -> dict[str, int | float]:
        return self.outcomes.outcome_summary()

    def refresh_floor_state(self) -> None:
        self.prefill_floor.minimum = self.min_prefill
        self.prefill_floor.on_event = self.on_floor_event
        self.prefill_floor.refresh()

    def floor_snapshot(self) -> dict:
        return self.prefill_floor.snapshot()

    def prefill_live(self) -> int:
        return len(self.live_instances(Role.PREFILL))

    def decode_live(self) -> int:
        return len(self.live_instances(Role.DECODE))

    def _prefill_candidates(self) -> list[Instance]:
        live = self.live_instances()
        return [i for i in live if i.role is Role.PREFILL] or live

    def prefill_admission_price(
        self, request: Request, inst: Instance, borrowed: Collection[str] | None = None
    ) -> float:
        """Price `inst`'s prefill for admission; `borrowed` is `prefill_pool`'s set when known."""
        if (
            not self.live_instances(Role.PREFILL)
            and inst.role is Role.DECODE
            and inst.decode
            and not (inst.iid in borrowed if borrowed is not None else self.borrows(request, inst))
        ):
            return float("inf")
        return self.cost(request, inst)[1]

    def role_placeable(self, role: Role) -> bool:
        return bool(self.role_pool(role, self.live_instances()))

    def prefill_ready_s(self, request: Request, inst: Instance) -> float:
        profile = self.profiles.get(inst.iid)
        if profile is None:
            return 0.0
        return resident_prefill_seconds(profile, inst) + prefill_seconds(profile, request)

    def cheapest_own_prefill(self, request: Request) -> float | None:
        """Resident queues and probation penalties are excluded."""
        floors = [
            prefill_seconds(profile, request)
            for inst in self._prefill_candidates()
            if (profile := self.profiles.get(inst.iid)) is not None
        ]
        return min(floors) if floors else None

    def pool_load(self, role: Role) -> float:
        """Resident work remains in phase feedback after an engine changes role.

        A decode-role engine whose resident prefill legs are all borrowed adds its prefill
        load to the prefill pool without counting as a prefill engine.
        """
        doing = [
            instance
            for instance in self.live_instances()
            if instance.role is role
            or (instance.prefill if role is Role.PREFILL else instance.decode)
        ]
        phase_load = self.prefill_load if role is Role.PREFILL else self.decode_load
        engines = (
            sum(
                not (
                    instance.role is Role.DECODE
                    and all(leg.borrowed for leg in instance.prefill.values())
                )
                for instance in doing
            )
            if role is Role.PREFILL
            else len(doing)
        )
        load = sum(phase_load(instance) for instance in doing)
        return load / (engines or len(doing)) if doing else 0.0

    def decode_exclusion(self, request: Request, exclude: Collection[str] = ()) -> set[str]:
        """A decode leg on its own producer reuses that engine's prefix cache and never pulls
        the KV handoff, so the producer holds the blocks until its lease expires.
        """
        producer = request.prefill_instance
        if request.phase is not Phase.DECODE or producer is None:
            return set()
        pool = self.role_pool(Role.DECODE, self.live_instances(exclude=set(exclude)))
        excluded = {producer} if any(inst.iid != producer for inst in pool) else set()
        stale = self.stale_handoff
        if stale is not None:
            held = {inst.iid for inst in pool if stale(producer, inst.iid)}
            if any(inst.iid not in excluded | held for inst in pool):
                excluded |= held
        return excluded

    def has_fresh_consumer(self, producer: str, decodes: list[Instance]) -> bool:
        stale = self.stale_handoff
        if stale is None:
            return True
        return any(d.iid != producer and not stale(producer, d.iid) for d in decodes)

    def _fresh_producers(self, candidates: list[Instance]) -> list[Instance]:
        if self.stale_handoff is None:
            return candidates
        decodes = self.role_pool(Role.DECODE, self.live_instances())
        fresh = [inst for inst in candidates if self.has_fresh_consumer(inst.iid, decodes)]
        return fresh or candidates

    def borrows(self, request: Request, inst: Instance) -> bool:
        """Return whether placement borrows decode-role `inst` for `request`'s prefill leg.

        With `prefill_on_decode`, it does when the engine is unpinned, not held for a switch,
        inside its envelope for the leg, and another live decode-pool engine can take the
        decode leg, which avoids its producer.
        """
        return self.lender(request)(inst)

    def lender(self, request: Request) -> Callable[[Instance], bool]:
        """Return `borrows` for `request`, with the fleet state read once."""
        if not self.prefill_on_decode:
            return lambda inst: False
        unusable = self.held_for_switch() | self.pinned
        decode_pool = {inst.iid for inst in self.role_pool(Role.DECODE, self.live_instances())}
        estimate = self.output_estimator()

        def borrows(inst: Instance) -> bool:
            return (
                inst.role is Role.DECODE
                and inst.iid not in unusable
                and len(decode_pool) > (inst.iid in decode_pool)
                and costs.decode_engine_prefill(
                    request,
                    inst,
                    monitor=self.monitor,
                    profiles=self.profiles,
                    slo=self.slo,
                    expected_output=estimate,
                )
            )

        return borrows

    def prefill_pool(
        self, request: Request, instances: list[Instance]
    ) -> tuple[list[Instance], set[str]]:
        """Return the prefill candidates among live `instances`, and the borrowed ones.

        With `prefill_on_decode`, the candidates are the prefill-role engines and the borrowed
        decode-role engines. When neither exists, they are the role pool without the setting.
        """
        pool = self.role_pool(Role.PREFILL, instances)
        if not self.prefill_on_decode:
            return pool, set()
        own = [inst for inst in pool if inst.role is Role.PREFILL]
        borrows = self.lender(request)
        borrowed = [inst for inst in instances if borrows(inst)]
        if not own and not borrowed:
            return pool, set()
        return own + borrowed, {inst.iid for inst in borrowed}

    def prefill_tier(
        self,
        request: Request,
        candidates: list[Instance],
        prices: dict[str, Cost],
        borrowed: Collection[str],
    ) -> list[Instance]:
        """Return the candidates placement chooses among.

        Borrowed engines take part only when no other candidate meets the TTFT budget and
        one of them does, or when every candidate is borrowed.
        """
        own = [inst for inst in candidates if inst.iid not in borrowed]
        if not borrowed or any(self.meets_slo(request, prices[inst.iid]) for inst in own):
            return own
        lent = [inst for inst in candidates if inst.iid in borrowed]
        if not own or any(self.meets_slo(request, prices[inst.iid]) for inst in lent):
            return lent
        return own

    def own_prefill(self, instances: list[Instance]) -> list[Instance]:
        """Return the prefill-role engines `prefill_pool` keeps among `instances`."""
        return [i for i in self.role_pool(Role.PREFILL, instances) if i.role is Role.PREFILL]

    def own_prefill_meets(
        self, request: Request, instances: list[Instance], probation: Collection[str] | None
    ) -> list[Instance] | None:
        """Return the prefill-role engines when one meets the TTFT budget and the setting is on.

        The prefill tier then holds only them, so no borrow check runs.
        """
        if not self.prefill_on_decode:
            return None
        own = self.own_prefill(instances)
        if any(self.meets_slo(request, self.cost(request, i, probation=probation)) for i in own):
            return own
        return None

    def settle_on_own(
        self,
        request: Request,
        instances: list[Instance],
        probation: Collection[str] | None,
        prices: dict[str, Cost] | None = None,
    ) -> tuple[list[Instance], dict[str, Cost]] | None:
        """Return the filtered prefill-role candidates and prices when placement keeps to them.

        With `prefill_on_decode`, placement keeps to the prefill-role engines when, after the
        switch-hold and fresh-producer filters, one meets the TTFT budget at its warm price
        and, for a request with cache evidence, at its cold price. No borrowed engine can
        then change the choice, so no borrow check runs. The warm prices land in `prices`.
        """
        if not self.prefill_on_decode:
            return None
        held = self.held_for_switch()
        kept = [i for i in self.own_prefill(instances) if i.iid not in held]
        if self.stale_handoff is not None:
            decodes = self.role_pool(Role.DECODE, self.live_instances())
            kept = [i for i in kept if self.has_fresh_consumer(i.iid, decodes)]
        prices = {} if prices is None else prices
        prices.update({i.iid: self.cost(request, i, probation=probation) for i in kept})
        if not any(self.meets_slo(request, prices[i.iid]) for i in kept):
            return None
        if request.cached_tokens:
            cold = self._cold_prices(request, kept, prices, probation)
            if not any(self.meets_slo(request, cold[i.iid]) for i in kept):
                return None
        return kept, prices

    def schedule(self, request: Request, exclude: set[str] | None = None) -> Instance:
        exclude = set(exclude or ())
        exclude |= self.decode_exclusion(request, exclude)
        instances = self.live_instances(exclude=exclude)
        if not instances:
            raise RuntimeError("no schedulable instances")

        probation = self.health.probation_set() if self.health is not None else None
        settled = None
        known: dict[str, Cost] = {}
        # Profiles model sequential prefill and batched decode.
        if request.phase is Phase.PREFILL:
            request.cache_placement = None
            # The envelope prices the leg with current cache evidence.
            self.recheck_evidence(request, 0.0)
            settled = self.settle_on_own(request, instances, probation, known)
        borrowed: set[str] = set()
        if settled is not None:
            candidates, prices = settled
        else:
            if request.phase is Phase.PREFILL:
                candidates, borrowed = self.prefill_pool(request, instances)
            else:
                candidates = self.role_pool(Role.DECODE, instances)
            if not candidates:
                raise RuntimeError("no schedulable instances for the pinned roles")
            held = self.held_for_switch()
            if held and any(inst.iid not in held for inst in candidates):
                candidates = [inst for inst in candidates if inst.iid not in held]
            if request.phase is Phase.PREFILL:
                candidates = self._fresh_producers(candidates)
            prices = {
                i.iid: known[i.iid]
                if i.iid in known
                else self.cost(request, i, probation=probation)
                for i in candidates
            }

        chosen, served = self._cheapest(request, candidates, prices, borrowed)
        if request.phase is Phase.PREFILL:
            request.borrowed = chosen.iid in borrowed
        if not served:
            # Admission decides over-budget placements.
            self.unserved += 1
        if request.phase is Phase.PREFILL and request.cached_tokens:
            request.cache_placement = self._cache_placement(
                request, chosen, candidates, prices, probation, borrowed
            )
        return chosen

    def hold_for_switch(self, iid: str) -> None:
        self.switch_holds = {iid: self._clock()}

    def renew_draining_holds(self) -> None:
        now = self._clock()
        for iid in self.held_for_switch():
            inst = self.monitor.instances.get(iid)
            if inst is not None and (inst.prefill or inst.decode):
                self.switch_holds[iid] = now

    def held_for_switch(self) -> set[str]:
        now = self._clock()
        self.switch_holds = {
            iid: at for iid, at in self.switch_holds.items() if now - at <= self.switch_hold_s
        }
        return set(self.switch_holds)

    def recheck_evidence(self, request: Request, fresh_s: float) -> None:
        if (
            request.phase is Phase.PREFILL
            and request.cached_tokens
            and self.recheck_cache_evidence is not None
        ):
            self.recheck_cache_evidence(request, fresh_s)

    def _cheapest(
        self,
        request: Request,
        candidates: list[Instance],
        prices: dict[str, Cost],
        borrowed: Collection[str] = frozenset(),
    ) -> tuple[Instance, bool]:
        tier = self.prefill_tier(request, candidates, prices, borrowed)
        eligible = [i for i in tier if self.meets_slo(request, prices[i.iid])]
        return min(eligible or tier, key=lambda i: (prices[i.iid], i.iid)), bool(eligible)

    def _cache_placement(
        self,
        request: Request,
        chosen: Instance,
        candidates: list[Instance],
        prices: dict[str, Cost],
        probation: Collection[str] | None,
        borrowed: Collection[str],
    ) -> dict[str, Any] | None:
        profile = self.profiles.get(chosen.iid)
        if profile is None:
            return None
        return {
            "placed_iid": chosen.iid,
            "placed_cached_tokens": request.cached_tokens.get(chosen.iid, 0),
            "evidence_sequence": request.cache_sequences.get(chosen.iid),
            "predicted_prefill_s": prefill_seconds(profile, request),
            "cold_prefill_s": profile.prefill_time(request.input_len),
            "cold_choice_iid": self._cold_choice(request, candidates, prices, probation, borrowed),
        }

    def _cold_choice(
        self,
        request: Request,
        candidates: list[Instance],
        prices: dict[str, Cost],
        probation: Collection[str] | None,
        borrowed: Collection[str],
    ) -> str:
        cold = self._cold_prices(request, candidates, prices, probation)
        return self._cheapest(request, candidates, cold, borrowed)[0].iid

    def _cold_prices(
        self,
        request: Request,
        candidates: list[Instance],
        prices: dict[str, Cost],
        probation: Collection[str] | None,
    ) -> dict[str, Cost]:
        # Without positive cache evidence on an engine, its warm price is its cold price.
        return {
            i.iid: (
                self.cost(request, i, warm=False, probation=probation)
                if _cached_on(i.iid, request, i.prefill.values())
                else prices[i.iid]
            )
            for i in candidates
        }

    def health_pass(self) -> None:
        if self.health is None:
            return
        for inst in self.monitor.instances.values():
            if inst.iid in self.ejected:
                continue
            if not self.monitor.decode_profile_eligible(inst.iid):
                self.health.pause_for_prefill(inst.iid)
                continue
            observed = self.monitor.current_token_interval(inst.iid) or self.monitor.stalled_gap(
                inst.iid
            )
            profile = self.profiles.get(inst.iid)
            if profile is None:
                continue
            # Compare with the profiled interval at the batch that decodes.
            requests, tokens = self.monitor.decoding(inst.iid)
            expected = profile.token_interval(tokens, requests)
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
    return request.cached_tokens.get(iid, 0) > 0 or any(
        r.cached_tokens.get(iid, 0) > 0 for r in residents
    )
