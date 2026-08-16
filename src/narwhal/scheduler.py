"""Arrow's global scheduler: Algorithms 1, 2 and 3 of arXiv:2505.11916.

Loads are ratios against the SLO target, never raw counts (Arrow §5.5).

Flip directions are asymmetric (Arrow §5.5). P->D fires both inline in `schedule` and
on the monitoring loop, and every P->D flip is gated by the cooldown. D->P fires
inline only and is never gated.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

from .health import DriftTracker
from .monitor import InstanceMonitor
from .profiler import Profile, ProfileStore
from .types import Instance, Phase, Request, Role

log = logging.getLogger("narwhal.scheduler")

# Compared lexicographically: the first component has priority over the second.
Cost = tuple[float, float]

# Consecutive failed legs before an instance leaves the candidate set: far
# enough from one transient to be evidence, near enough to stop a dead
# instance from taking the fleet.


@dataclass
class Flip:
    """One role change, and what it was carrying when it happened.

    the study's methodology §C asks for the in-flight count at each migration, because a
    flip that strands work costs more than the relabel it looks like.
    """

    at: float
    iid: str
    to: Role
    by: str
    prefill_inflight: int
    decode_inflight: int
    # §E: the role change is a relabel, so the cost is the work it caught still
    # finishing under the old role. None until that work has gone.
    drained_s: float | None = None


@dataclass
class SLO:
    """The targets every decision is taken against."""

    ttft_s: float
    tpot_s: float


@dataclass
class Thresholds:
    """Arrow §5.5's flipping timing constants.

    `expand` and `shrink` are pool loads, so 1.0 is exactly at target.
    `cooldown_s` guards P->D only.
    """

    expand: float = 1.0
    shrink: float = 0.5
    cooldown_s: float = 10.0
    # Arrow §5.5 fires Algorithm 2 when the load "exceeds a threshold over a period of
    # time", not on one crossing. The Arrow paper names no count; the study's methodology §F
    # sweeps {1, 2, 3, 5} consecutive over-threshold ticks to tune it.
    sustained_intervals: int = 3
    # the study's methodology §F's dwell: an instance that just flipped may not flip
    # again, in either direction, for this long. 0 is the Arrow paper's behaviour.
    # The one-sided cooldown damps the pool's P->D rate; the dwell pins the
    # *instance*, which is what stops two algorithms trading the same engine
    # at a boundary.
    dwell_s: float = 0.0
    # The cooldown yields to a sustained regime flip: decode load at this
    # multiple of `expand` WHILE prefill sits at or below `shrink`, held for
    # `sustained_intervals` consecutive monitoring passes. The two-sided
    # condition is a hardware lesson from the bypass splice campaign: a
    # global spike raises both loads, and a decode-only trigger strips the
    # prefill pool mid-flood - halving goodput on the falsifying run. 0
    # disables the bypass, which is the Arrow paper's behaviour; enabled values
    # sit at or above 1.
    panic_ratio: float = 0.0


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
        prefill_affinity: bool = False,
        flip_offline_s: float = 0.0,
        prefix_coop: bool = False,
        prefix_halflife_s: float = 60.0,
        health: DriftTracker | None = None,
        pinned: frozenset[str] = frozenset(),
        min_prefill: int = 1,
    ) -> None:
        self.monitor = monitor
        self.profiles = profiles
        self.slo = slo
        self.th = thresholds or Thresholds()
        self._clock = clock
        # The drift instrument: per-engine drift against its own profile.
        # None keeps the reactive-only behaviour of the Arrow paper's §5.6.
        self.health = health
        # Engines whose role is pinned by config: no flip path moves them.
        # Empty means every engine is fair game, exactly as before.
        self.pinned = pinned
        # The prefill pool's floor; 1 is the Arrow paper's never-empty guard.
        self.min_prefill = max(1, min_prefill)
        # Process start counts as the last P->D flip. Seeded with -inf, the
        # cooldown check reads `inf < cooldown_s`, which is false for every
        # cooldown, so the first P->D was ungated no matter how large the
        # cooldown - and a pinned arm relies on a large cooldown to be pinned.
        self._last_p2d_flip = clock()
        self.panic_bypasses = 0
        # Consecutive monitoring passes with the two-sided panic condition
        # true; the bypass requires `sustained_intervals` of them, the same
        # discipline Algorithm 2 applies to its own trigger.
        self._panic_sustained = 0
        # When each instance last flipped, for the dwell. Empty means never:
        # a fresh instance owes no dwell.
        self._last_flip: dict[str, float] = {}
        # Ejected instances, each against the time it was last probed. Arrow §5.2
        # assumes every instance can serve either phase, so the cost functions
        # have no term for one that answers nothing: a refused leg leaves no
        # residency behind, which prices the instance at the fleet minimum
        # and wins it every argmin. Ejection is what removes it from the
        # argument entirely.
        self.ejected: dict[str, float] = {}
        self._failures: Counter[str] = Counter()
        self._eject_after = eject_after
        # A target-state controller that owns the pools suppresses this
        # scheduler's own flips (Algorithm 1 step 3 and Algorithm 2): two
        # controllers over one fleet fight.
        self.controller_owns_flips = False
        self._soft_failures: Counter[str] = Counter()
        # Consecutive silent /health sweeps per live instance, and when the
        # last sweep ran. Both are this process's own reckoning of who is
        # answering, so neither survives a handoff: a replacement re-earns it
        # on its first sweep rather than inheriting a suspicion it cannot check.
        self.liveness_misses: dict[str, int] = {}
        self._last_sweep = 0.0
        # The flips and refusals lists are telemetry, not state the scheduler
        # reads; uncapped they grow for the life of the process.
        self._flip_history = flip_history
        self.flips: list[Flip] = []
        # Requests that reached step 3 with nothing meeting the SLO, and the
        # flips that step then declined. Empty counters say the pool never
        # needed to move, which is a different story from one that could not.
        self.unserved = 0
        self.flips_refused: list[tuple[float, str, str]] = []
        self._sustained = 0
        # The observation-only efficiency gauge: per-placement regret -
        # the chosen candidate's scalar cost over that placement's own
        # cheapest option. A windowed matching optimum is the offline
        # replay's estimator and is ill-defined live (each placement
        # snapshots its own state); regret is the live-computable
        # component. Nothing reads these for control.
        from collections import deque

        self._regrets: deque[float] = deque(maxlen=120)
        # The affinity ablation: reintroduce the selfish caching game on
        # purpose. When on, a prefill leg whose prefix was last served by a
        # live engine goes back to that engine unconditionally - private
        # benefit taken regardless of congestion, which is exactly the
        # equilibrium play the experiment prices. Off is the shipped
        # behaviour: statelessness, no cache ownership.
        self.prefill_affinity = prefill_affinity
        self.flip_offline_s = flip_offline_s
        # iid -> monotonic time its relaunch window ends (#flip_offline_s).
        self.offline_until: dict[str, float] = {}
        # Cooperative reuse: the shared prefix priced as a discount
        # inside Algorithm 1's cost, never an override, so reuse wins ties
        # and loses conflicts the resident work prices larger than the
        # saving. Warmth decays on a half-life because engines evict
        # silently and a stale map must fade rather than lie.
        self.prefix_coop = prefix_coop
        self.prefix_halflife_s = prefix_halflife_s
        # (at, ttft_ok, tpot_ok) per completed request: the planner's
        # closed-loop signal. Failures count as TTFT misses.
        from collections import deque

        self.outcomes: deque[tuple[float, bool, bool]] = deque(maxlen=4096)
        from collections import OrderedDict

        self._affinity: OrderedDict[int, str] = OrderedDict()
        # (prefix_key, iid) -> (cached tokens, last touch): every engine
        # known to hold the key, not one home per key. Placement onto an
        # unrecorded engine teaches the map; a pile on one warm engine has
        # no reason to form - every recorded holder discounts alike, so
        # balance emerges with the savings kept. Capped by recency; decay
        # stands in for the engine-side eviction the router cannot see.
        self._warm: OrderedDict[tuple[int, str], tuple[int, float]] = OrderedDict()

    # -- availability ---------------------------------------------------

    def record_failure(self, iid: str, *, connection_shaped: bool = True) -> str | None:
        """Count one failed leg; report "eject", "verify" or None.

        Two failure shapes, two verdicts: a connection-shaped failure
        (refused, unreachable) is how a dead engine presents, and three in a
        row eject. A timeout-shaped failure is how an overloaded engine
        presents - the 504 tail is load, and load is the scheduler's job -
        so three in a row return "verify": the caller probes /health and
        ejects only if that fails too, which is the wedged-listener case.
        Without the distinction the breaker chattered 47 readmission cycles
        on a healthy engine under one flood phase.

        The last live instance is never ejected: a router with nowhere to
        send a request answers exactly as it does when it sends the request
        to a dead engine, and an empty candidate set leaves nothing to probe
        from.
        """
        counter = self._failures if connection_shaped else self._soft_failures
        counter[iid] += 1
        if iid in self.ejected or counter[iid] < self._eject_after:
            return None
        if not connection_shaped:
            self._soft_failures.pop(iid, None)
            return "verify"
        if len(self.ejected) + 1 >= len(self.monitor.instances):
            return None
        self.ejected[iid] = self._clock()
        self._purge_warm(iid)
        return "eject"

    def eject(self, iid: str) -> bool:
        """Eject outright: the verify probe failed, so the engine is gone.

        The last-live guard still holds.
        """
        if iid in self.ejected or len(self.ejected) + 1 >= len(self.monitor.instances):
            return False
        self.ejected[iid] = self._clock()
        self._purge_warm(iid)
        return True

    def _purge_warm(self, iid: str) -> None:
        """A dead engine's caches are dead too; its records go with them."""
        for key in [k for k in self._warm if k[1] == iid]:
            del self._warm[key]

    def record_answer(self, iid: str) -> None:
        """The instance answered, so clear the breaker and take it back."""
        self._failures.pop(iid, None)
        self._soft_failures.pop(iid, None)
        self.liveness_misses.pop(iid, None)
        self.ejected.pop(iid, None)

    def sweep_due(self, after_s: float) -> bool:
        """Whether a liveness sweep is due, marking it run when it is."""
        now = self._clock()
        if now - self._last_sweep < after_s:
            return False
        self._last_sweep = now
        return True

    def probe_due(self, after_s: float) -> list[str]:
        """Ejected instances this far past their last probe, marked probed."""
        now = self._clock()
        due = [iid for iid, at in self.ejected.items() if now - at >= after_s]
        for iid in due:
            self.ejected[iid] = now
        return due

    # -- §5.3 cost functions --------------------------------------------

    def cost(self, request: Request, inst: Instance) -> Cost:
        """`GetCost(r, i)`, §5.3.

        Prefill: `(sum L(rd) for rd in D, sum T(rp, i) for rp in P + {r})`.
        Decode:  `(sum L(rp) for rp in P, sum L(rd) for rd in D + {r} - MT(i))`.
        """
        profile = self.profiles.get(inst.iid)
        if profile is None:
            raise KeyError(f"no profile for instance {inst.iid}; profile before scheduling")

        # An engine on probation prices above itself, so argmin drains
        # new work from it while peers have room. Prefill costs seconds, so
        # the penalty is a flat addition; decode costs tokens, converted at
        # the TPOT SLO rate so the two legs carry the same deterrent.
        penalty = 0.0
        if self.health is not None and inst.iid in self.health.probation_set():
            penalty = self.health.penalty_s

        if request.phase is Phase.PREFILL:
            # Resident work is priced net of the reuse each queued request
            # will realize here: a queue full of hits is a cheap queue, and
            # pricing it at full prefill would scare placements off a warm
            # engine exactly when stacking it is cheapest for everyone.
            resident = sum(
                max(0.0, profile.prefill_time(r.input_len) - self._reuse(inst, r, profile))
                for r in inst.prefill.values()
            )
            own = max(
                0.0, profile.prefill_time(request.input_len) - self._reuse(inst, request, profile)
            )
            return (float(inst.decode_tokens()), resident + own + penalty)

        headroom = profile.max_tokens(self.slo.tpot_s)
        return (
            float(inst.prefill_tokens()),
            float(inst.decode_tokens() + request.length) - headroom + penalty / self.slo.tpot_s,
        )

    def _reuse(self, inst: Instance, request: Request, profile: Profile) -> float:
        """The prefix work this engine already holds, decayed by freshness.

        A warm hit saves the cached tokens' prefill time, discounted by a
        half-life on the last touch: engines evict silently, so credit for a
        cache the router cannot inspect must fade. Records are per engine:
        any engine the key has been computed on claims the saving, and an
        engine that has never seen it scores zero.
        """
        if not self.prefix_coop or request.prefix_key is None:
            return 0.0
        rec = self._warm.get((request.prefix_key, inst.iid))
        if rec is None:
            return 0.0
        tokens, touched = rec
        # Claim only what placement records could prove: the shortest span
        # this engine computed under the key, clipped to the request, and
        # to whatever span its own identity is proven over. Tails lie.
        proven = request.prefix_len if request.prefix_len is not None else request.input_len
        claim = min(tokens, request.input_len, proven)
        warmth = 0.5 ** ((self._clock() - touched) / self.prefix_halflife_s)
        return warmth * profile.prefill_time(claim)

    def meets_slo(self, request: Request, cost: Cost, *, ttft_margin: float = 0.0) -> bool:
        """§5.3: the second cost component against the TTFT target, or zero.

        `ttft_margin` widens the prefill budget by that fraction, so a
        landing priced as noise right at the boundary is not refused as a
        miss. The deadline stays the SLO; the margin is hysteresis.
        """
        if request.phase is Phase.PREFILL:
            return cost[1] <= self.slo.ttft_s * (1.0 + ttft_margin)
        return cost[1] <= 0.0

    def _prefill_candidates(self) -> list[Instance]:
        """Algorithm 1's candidate rule: the prefill pool, or every live
        instance when the pool is empty. Ejected and offline never qualify.
        """
        live = [
            i
            for i in self.monitor.instances.values()
            if i.iid not in self.ejected and not self._offline(i.iid)
        ]
        return [i for i in live if i.role is Role.PREFILL] or live

    def cheapest_prefill_price(self, request: Request) -> float | None:
        """The lowest TTFT priced at placement over live prefill candidates.

        None when nothing is live at all. This is the door's view of the
        fleet: what the cheapest landing for this request would cost it,
        before any flip is attempted.
        """
        candidates = self._prefill_candidates()
        if not candidates:
            return None
        return min(self.cost(request, i)[1] for i in candidates)

    def cheapest_own_prefill(self, request: Request) -> float | None:
        """The request's own prefill on the cheapest candidate, priced alone.

        `cheapest_prefill_price` quotes the landing available now, resident
        queue and probation penalty folded in. Both of those drain. This
        quotes the part that does not: the prefill work the request itself
        brings, net of the reuse a candidate can prove. A floor already over
        the TTFT budget is a refusal no amount of waiting reverses, so the
        door can tell the two apart. None when no live candidate carries a
        profile.
        """
        floors = [
            max(0.0, profile.prefill_time(request.input_len) - self._reuse(inst, request, profile))
            for inst in self._prefill_candidates()
            if (profile := self.profiles.get(inst.iid)) is not None
        ]
        return min(floors) if floors else None

    # -- Arrow §5.5 load ------------------------------------------------------

    def prefill_load(self, inst: Instance) -> float:
        """Arrow §5.5: estimated prefill time on this instance over the TTFT SLO.

        Prefill work is resident only from dispatch to o1, so at low rates the
        set is empty at most instants and a value read off it at pass time
        aliases to zero. The interval average is what was resident over the
        completed interval, the same footing decode load reads on. The larger
        of the two is used because the shrink trigger's failure direction is
        under-reading: a zero it should not have lends a prefill instance
        away.
        """
        profile = self.profiles.get(inst.iid)
        if profile is None:
            return 0.0
        resident = sum(profile.prefill_time(r.input_len) for r in inst.prefill.values())
        return max(resident, self.monitor.mean_prefill_price(inst.iid)) / self.slo.ttft_s

    def decode_load(self, inst: Instance) -> float:
        """Arrow §5.5: observed inter-token latency over the TPOT SLO, above the floor.

        An instance that generated nothing in the interval has an empty average,
        which reads 0 and makes stalling look like idling. The longest gap a
        generating request has waited is the floor that average is missing.

        The observed interval is normalized over the profiled generation floor,
        `token_interval(0)`: an engine ticking at its natural cadence is idle,
        not half-loaded. A raw ratio against a target within about twice the
        cadence sits above the shrink threshold on an unloaded pool, which
        arms Algorithm 2's shrink trigger by default and refuses every one of
        Algorithm 1's D->P recovery flips. Above the floor, 0 means idle and
        1.0 means at-SLO at any SLO tightness.
        """
        if not inst.decode:
            return 0.0
        observed = max(
            self.monitor.mean_token_interval(inst.iid), self.monitor.stalled_gap(inst.iid)
        )
        profile = self.profiles.get(inst.iid)
        floor = profile.token_interval(0) if profile is not None else 0.0
        if floor >= self.slo.tpot_s:
            # No headroom at any batch (§5.3's MT reads 0): the raw ratio is
            # all that is left to say how far past the target it runs.
            return observed / self.slo.tpot_s
        return max(0.0, observed - floor) / (self.slo.tpot_s - floor)

    def pool_load(self, role: Role) -> float:
        """Mean load over the instances doing that phase's work (Arrow §5.5).

        Arrow §5.5 defines the prefill load "of an instance" from its resident prefill
        work, not from its label, and Algorithm 1 costs every instance rather
        than the matching pool, so prefill lands wherever it is cheapest. Reading
        prefill load off the prefill pool alone therefore misses the work that
        Algorithm 1 put on a decode-labelled instance, and reports an idle
        prefill pool while TTFT is missing. An empty set reads 0.
        """
        if role is Role.PREFILL:
            doing = [
                i
                for i in self.monitor.instances.values()
                if (i.role is role or i.prefill)
                and i.iid not in self.ejected
                and not self._offline(i.iid)
            ]
            return sum(self.prefill_load(i) for i in doing) / len(doing) if doing else 0.0
        pool = [
            i
            for i in self.monitor.pool(role)
            if i.iid not in self.ejected and not self._offline(i.iid)
        ]
        if not pool:
            return 0.0
        return sum(self.decode_load(i) for i in pool) / len(pool)

    # -- Arrow §5.5 Algorithm 3 -----------------------------------------------

    def note_outcome(self, ttft_ok: bool, tpot_ok: bool) -> None:
        """One completed request's verdict against the SLO."""
        self.outcomes.append((self._clock(), ttft_ok, tpot_ok))

    def note_ttft(self, iid: str, observed_s: float, predicted_s: float) -> None:
        """One resolved prefill placement's drift residual.

        Observed covers placement to o1; predicted is the second component of
        the cost Algorithm 1 quoted when it chose `iid`. The ratio is the
        tracker's input; bias common to every engine cancels against the own
        profile and only the over-band direction drifts.
        """
        if self.health is None:
            return
        self.health.note_ttft(iid, observed_s, predicted_s)

    def _offline(self, iid: str) -> bool:
        """True while `iid` is inside its relaunch window (`flip_offline_s`)."""
        return self._clock() < self.offline_until.get(iid, 0.0)

    def flip_cost(self, inst: Instance) -> Cost:
        """Arrow §5.5's flipping cost.

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

    def flip(self, target: Role, by: str = "?") -> Instance | None:
        """Algorithm 3. Moves one instance into `target`'s pool, or None.

        Never empties the source pool: Algorithm 3 guards on `|S| > 1`.
        """
        take_from = Role.DECODE if target is Role.PREFILL else Role.PREFILL
        now = self._clock()

        if target is Role.DECODE and now - self._last_p2d_flip < self.th.cooldown_s:
            if not self._panic_now():
                return None
            self.panic_bypasses += 1
            log.info(
                "PANIC BYPASS: decode %.2f >= %.1fx expand with prefill %.2f <= shrink, "
                "sustained %d passes - the cooldown yields",
                self.pool_load(Role.DECODE),
                self.th.panic_ratio,
                self.pool_load(Role.PREFILL),
                self._panic_sustained,
            )

        # An ejected instance is not in `S`: relabelling one adds no capacity
        # to the target pool and spends the cooldown that guards P->D.
        live = [
            i
            for i in self.monitor.pool(take_from)
            if i.iid not in self.ejected and not self._offline(i.iid)
        ]
        if len(live) <= 1:
            return None
        # The prefill floor counts the whole live pool: a pinned or dwelling
        # engine still holds its seat even though it cannot be the mover.
        if take_from is Role.PREFILL and len(live) - 1 < self.min_prefill:
            return None
        pool = [i for i in live if i.iid not in self.pinned]
        if not pool:
            return None
        if self.th.dwell_s > 0.0:
            rested = [
                i
                for i in pool
                if now - self._last_flip.get(i.iid, float("-inf")) >= self.th.dwell_s
            ]
            # The pool-size guard still sees the whole pool: a dwelling
            # instance is unavailable, not absent.
            if not rested:
                return None
            pool = rested

        chosen = min(pool, key=self.flip_cost)
        chosen.role = target
        if self.flip_offline_s > 0.0:
            self.offline_until[chosen.iid] = now + self.flip_offline_s
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
            )
        )
        del self.flips[: -self._flip_history]
        # The one line that says the fleet adapted. `/arrow/state` holds the
        # authoritative record; this puts the event in the log beside the
        # per-pass loop lines so a trajectory reads out of one file.
        log.info(
            "FLIP %s %s -> %s | carrying %dP %dD",
            by,
            chosen.iid,
            target.value,
            len(chosen.prefill),
            len(chosen.decode),
        )
        return chosen

    def settle_drains(self) -> None:
        """Close the drain on any flip whose caught work has finished (§E)."""
        now = self._clock()
        for f in self.flips:
            if f.drained_s is not None:
                continue
            inst = self.monitor.instances.get(f.iid)
            if inst is None:
                continue
            stale = inst.decode if f.to is Role.PREFILL else inst.prefill
            if not stale:
                f.drained_s = now - f.at

    # -- §5.3 Algorithm 1 -----------------------------------------------

    def schedule(self, request: Request, exclude: set[str] | None = None) -> Instance:
        """Algorithm 1, SLO-aware global request scheduling.

        `exclude` drops instances from every step, including the first branch.
        It is not in the Arrow paper, which assumes any instance can transfer KV to any
        other (Arrow §5.2); it exists so a caller can re-schedule around an endpoint
        that has just failed.
        """
        exclude = exclude or set()
        instances = [
            i
            for i in self.monitor.instances.values()
            if i.iid not in exclude and i.iid not in self.ejected and not self._offline(i.iid)
        ]
        if not instances:
            raise RuntimeError("no schedulable instances")

        # 1. Prefill instance already flipped to decode: no KV transfer needed.
        if (
            request.phase is Phase.DECODE
            and request.prefill_instance
            and request.prefill_instance not in exclude
            and request.prefill_instance not in self.ejected
            and not self._offline(request.prefill_instance)
        ):
            prior = self.monitor.instances.get(request.prefill_instance)
            if prior is not None and prior.role is Role.DECODE:
                return prior

        # The pool that does this phase's work. Arrow §4.1 assumes "prefill instances
        # process requests sequentially, while decode instances maximize batch
        # size", which is what the profile is measured against, so a prediction
        # for an instance batching the other phase is calibrated wrong. Arrow §5.5
        # reads the same way: the D->P trigger fires "when the scheduler
        # predicts that the current prefill instances cannot meet the TTFT SLO".
        want = Role.PREFILL if request.phase is Phase.PREFILL else Role.DECODE
        candidates = [i for i in instances if i.role is want] or instances
        costs = {i.iid: self.cost(request, i) for i in candidates}

        # 1b. The affinity ablation's selfish move: the warm engine wins
        #     outright when it is a live candidate, costs be damned.
        if (
            self.prefill_affinity
            and request.phase is Phase.PREFILL
            and request.prefix_key is not None
        ):
            warm = self._affinity.get(request.prefix_key)
            for i in candidates:
                if i.iid == warm:
                    return i

        # 2. Lowest-cost instance that also meets the SLO.
        eligible = [i for i in candidates if self.meets_slo(request, costs[i.iid])]
        if eligible:
            chosen = min(eligible, key=lambda i: costs[i.iid])
            if request.phase is Phase.PREFILL:
                self._poa_observe(candidates, costs, chosen)
            return chosen

        # 3. Nothing satisfies it, so try to flip. P->D proceeds unguarded;
        #    D->P is refused unless decode load is low (Algorithm 1 line 13).
        #
        #    "Low" is `shrink`, not `expand`. Algorithm 2's second trigger fires
        #    at `LP <= shrink <= LD`, so reading it as `expand` leaves decode
        #    load between the two thresholds where that trigger and this flip
        #    are both legal, and the pool reverses on the same instance. Arrow §5.5
        #    introduces the cooldown "to prevent oscillation in instance
        #    assignment", which an overlapping window undoes.
        wants_decode = request.phase is Phase.DECODE
        ld = self.pool_load(Role.DECODE)
        self.unserved += 1
        if self.controller_owns_flips:
            return min(candidates, key=lambda i: costs[i.iid])
        if wants_decode or ld < self.th.shrink:
            target = Role.DECODE if wants_decode else Role.PREFILL
            flipped = self.flip(target, "algorithm1")
            if flipped is not None and flipped.iid not in exclude:
                return flipped
            self.flips_refused.append((self._clock(), target.value, "cooldown or pool size"))
        else:
            self.flips_refused.append(
                (self._clock(), "prefill", f"decode load {ld:.2f} >= shrink {self.th.shrink}")
            )
        del self.flips_refused[: -self._flip_history]

        # 4. Fall back to the cheapest instance regardless of SLO.
        return min(candidates, key=lambda i: costs[i.iid])

    def queue_replacements(
        self, *, slack_s: float, limit: int | None = None, max_per_request: int = 1
    ) -> list[tuple[str, str, str]]:
        """Queued prefill legs whose staying price has missed the TTFT budget.

        A leg without its first token has no migrated state, so moving it is
        a re-dispatch priced by the same cost model as first placement, not
        a migration. Emits (rid, from, to) when the leg's price where it sits
        is over budget while a live peer meets it with `slack_s` to spare -
        the slack is the hysteresis that keeps a marginal move from firing.
        Deepest misses order the list; `limit` caps how many head it, for
        callers whose every candidate is applicable - prices snapshot the
        pass, so an unbounded move list would invert the skew rather than
        drain it. A leg moves at most `max_per_request` times: work that
        fits nowhere must not ping-pong.
        """
        live = [
            i
            for i in self.monitor.instances.values()
            if i.iid not in self.ejected and not self._offline(i.iid)
        ]
        if len(live) < 2:
            return []
        pool = [i for i in live if i.role is Role.PREFILL]
        targets = pool or live
        moves: list[tuple[float, str, str, str]] = []
        for inst in live:
            for rid, req in list(inst.prefill.items()):
                if req.replaced >= max_per_request:
                    continue
                # `cost` prices a not-yet-resident request as set-plus-self;
                # this leg is already in the set, so staying is the resident
                # price alone - adding the leg again would double-count it.
                profile = self.profiles.get(inst.iid)
                if profile is None:
                    continue
                # Net of the reuse each resident leg realizes here,
                # the same footing admission priced it on: a warm queue of
                # hits the door admitted under budget must not read as over
                # budget to this pass, or every pass cancels and re-drives
                # warm legs for nothing.
                staying = sum(
                    max(0.0, profile.prefill_time(r.input_len) - self._reuse(inst, r, profile))
                    for r in inst.prefill.values()
                )
                if staying <= self.slo.ttft_s:
                    continue
                peers = [i for i in targets if i.iid != inst.iid]
                if not peers:
                    continue
                best = min(peers, key=lambda i: self.cost(req, i))
                if self.cost(req, best)[1] <= self.slo.ttft_s - slack_s:
                    moves.append((staying, rid, inst.iid, best.iid))
        moves.sort(reverse=True)
        return [(rid, src, dst) for _, rid, src, dst in moves[:limit]]

    def schedule_batch(self, requests: list[Request]) -> dict[str, Instance]:
        """Joint prefill placement for a gathered window.

        The same cost pairs Algorithm 1 prices, assigned as an exact
        min-cost matching (one request per engine per window; the window
        is bounded by the fleet size). Requests beyond the matching -
        an over-full window, or a fleet with more requests than live
        candidates - fall through to `schedule` one at a time, so the
        batched mode degrades to greedy rather than refusing.
        """
        from itertools import permutations

        out: dict[str, Instance] = {}
        pending = list(requests)
        while pending:
            chunk = pending[: max(1, len(self._batch_candidates()))]
            pending = pending[len(chunk) :]
            candidates = self._batch_candidates()
            if len(chunk) > 1 and len(candidates) >= len(chunk):
                costs = [[self.cost(r, c) for c in candidates] for r in chunk]
                best, best_perm = None, None
                for perm in permutations(range(len(candidates)), len(chunk)):
                    total = sum(
                        costs[k][perm[k]][0] + costs[k][perm[k]][1] for k in range(len(chunk))
                    )
                    if best is None or total < best:
                        best, best_perm = total, perm
                if best_perm is None:
                    raise RuntimeError(
                        "batch gate priced no assignment: fewer candidates than the chunk"
                    )
                for k, r in enumerate(chunk):
                    out[r.rid] = candidates[best_perm[k]]
                continue
            for r in chunk:
                out[r.rid] = self.schedule(r)
        return out

    def _batch_candidates(self) -> list[Instance]:
        """Live prefill-pool candidates for a batched window.

        The offline filter matches the greedy path's candidate rule: an
        instance inside its relaunch window serves nothing, so a batch must
        not be matched onto it either.
        """
        pool = [
            i
            for i in self.monitor.pool(Role.PREFILL)
            if i.iid not in self.ejected and not self._offline(i.iid)
        ]
        return pool or [
            i
            for i in self.monitor.instances.values()
            if i.iid not in self.ejected and not self._offline(i.iid)
        ]

    def remember(self, request: Request, chosen: Instance) -> Instance:
        """Record what this placement teaches about the prefix.

        Called at dispatch, not at scheduling: a placement the admission
        door then refuses computes nothing, and recording it would credit
        an engine with warmth it never earned - a poisoned record that
        underprices every later landing of the key (and the refusal
        decision itself). The affinity ablation reads the pairing
        unconditionally; the cooperative term reads the same
        pairing as warmth - which engine now holds the key, how much of
        it, and how fresh. Both maps are capped so neither grows with the
        trace; the repair from a forgotten record is one cold placement,
        paid once.
        """
        if request.phase is not Phase.PREFILL or request.prefix_key is None:
            return chosen
        if self.prefill_affinity:
            self._affinity[request.prefix_key] = chosen.iid
            self._affinity.move_to_end(request.prefix_key)
            while len(self._affinity) > 256:
                self._affinity.popitem(last=False)
        if self.prefix_coop:
            prev = self._warm.get((request.prefix_key, chosen.iid))
            # The shortest sequence this engine has computed under the key
            # caps the certainly-shared span: a key promises a head, nothing
            # about the tails.
            tokens = request.input_len if prev is None else min(prev[0], request.input_len)
            self._warm[(request.prefix_key, chosen.iid)] = (tokens, self._clock())
            self._warm.move_to_end((request.prefix_key, chosen.iid))
            while len(self._warm) > 256:
                self._warm.popitem(last=False)
        return chosen

    # -- Arrow §5.5 Algorithm 2 -----------------------------------------------

    def monitoring_pass(self) -> Instance | None:
        """Algorithm 2, one pass of the monitoring loop.

        Flips P->D only. Two triggers (Arrow §5.5): decode over its expand threshold,
        or prefill idle while decode is not. `schedule` carries the other P->D
        path and the only D->P path.
        """
        lp = self.pool_load(Role.PREFILL)
        ld = self.pool_load(Role.DECODE)
        triggered = ld >= self.th.expand or (lp <= self.th.shrink <= ld)
        self._sustained = self._sustained + 1 if triggered else 0
        self._note_panic(lp, ld)

        flipped = None
        if self._sustained >= self.th.sustained_intervals:
            flipped = self.flip(Role.DECODE, "algorithm2")
            if flipped is not None:
                self._sustained = 0
        self.health_pass()
        self.settle_drains()
        self.monitor.roll_interval()
        return flipped

    def health_pass(self) -> None:
        """One drift pass: sample every live engine, apply verdicts.

        Controller-independent, like readmission: the planner replaces
        Algorithm 2's flip trigger, not the health instrument, so the server
        loop calls this on both controller branches (a planner-controlled
        fleet losing its early drift instrument was exactly the bug).
        """
        if self.health is None:
            return
        for inst in self.monitor.instances.values():
            if inst.iid in self.ejected:
                continue
            observed = max(
                self.monitor.mean_token_interval(inst.iid),
                self.monitor.stalled_gap(inst.iid),
            )
            profile = self.profiles.get(inst.iid)
            if profile is None:
                continue
            # The floor alone would convict the busiest healthy engine:
            # a full batch ticks slower by profile, so the prediction is
            # the engine's own curve at the batch it actually carries.
            expected = profile.token_interval(inst.decode_tokens())
            if observed > 0.0 and expected > 0.0:
                self.health.note(inst.iid, observed / expected)
        for verdict, iid in self.health.tick():
            if verdict == "evict" and self.eject(iid):
                self.health.evicted(iid)
                log.warning(
                    "ejected %s on sustained drift; probes readmit it the moment it answers",
                    iid,
                )
            elif verdict == "evict":
                log.warning(
                    "health: %s drifts past the band but is the last instance; "
                    "probation stands, eviction refused",
                    iid,
                )

    def _poa_observe(self, candidates: list[Instance], costs: dict, chosen: Instance) -> None:
        """Record one placement's regret for the PoA gauge."""
        if len(candidates) < 2:
            return
        scalars = {i.iid: costs[i.iid][0] + costs[i.iid][1] for i in candidates}
        floor = min(scalars.values())
        if floor > 0:
            self._regrets.append(scalars[chosen.iid] / floor)

    def placement_regret(self) -> float | None:
        """Median per-placement regret over recent placements, or None."""
        if not self._regrets:
            return None
        vals = sorted(self._regrets)
        return vals[len(vals) // 2]

    def regime(self) -> str:
        """The prequel's three regimes, from live pool loads."""
        lp = self.pool_load(Role.PREFILL)
        ld = self.pool_load(Role.DECODE)
        peak = max(lp, ld)
        if peak >= 2.0 * self.th.expand:
            return "saturated"
        if peak >= self.th.expand:
            return "transitional"
        return "subcritical"

    def _note_panic(self, lp: float, ld: float) -> None:
        """Advance or reset the panic signal from one monitoring pass.

        The condition is two-sided by measurement, not caution: decode past
        the panic multiple AND prefill at or below `shrink` - a regime flip.
        A global spike raises both loads and must not count (the ledger's
        falsified one-sided run stripped prefill mid-flood exactly that way).
        """
        armed = (
            self.th.panic_ratio > 0.0
            and ld >= self.th.panic_ratio * self.th.expand
            and lp <= self.th.shrink
        )
        self._panic_sustained = self._panic_sustained + 1 if armed else 0

    def _panic_now(self) -> bool:
        """True when the sustained two-sided condition licenses a bypass.

        Re-verified against live loads at fire time: the monitoring counter
        says the regime held; the fresh read says it still does.
        """
        if self.th.panic_ratio <= 0.0:
            return False
        if self._panic_sustained < self.th.sustained_intervals:
            return False
        return (
            self.pool_load(Role.DECODE) >= self.th.panic_ratio * self.th.expand
            and self.pool_load(Role.PREFILL) <= self.th.shrink
        )
