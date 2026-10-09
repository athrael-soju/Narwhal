"""Fuzz admission, projection and placement against their cb41445 implementations."""

import copy
import heapq
import math
import random
import tempfile
import unittest
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

from narwhal.profiling.model import Profile
from narwhal.profiling.store import ProfileStore
from narwhal.scheduling.control import SLO
from narwhal.scheduling.costs import Cost
from narwhal.scheduling.demand import DemandModel
from narwhal.scheduling.health import DriftTracker
from narwhal.scheduling.monitor import InstanceMonitor
from narwhal.scheduling.prefill import prefill_seconds, resident_prefill_seconds
from narwhal.scheduling.scheduler.occupancy import (
    decode_admits,
    decode_occupancy,
    decode_span,
    peak_holds,
)
from narwhal.scheduling.scheduler.placement import GlobalScheduler
from narwhal.scheduling.scoring import PrefillProjection, SplitScorer
from narwhal.types import Instance, Phase, Request, Role
from tests.fixtures import profile, warm

# Reference implementations, copied from cb41445 with `self` bound to an argument.

ReferenceSpan = tuple[float, float, int]


def reference_cost(
    scheduler: GlobalScheduler, request: Request, inst: Instance, *, warm: bool = True
) -> Cost:
    profile = scheduler.profiles.get(inst.iid)
    if profile is None:
        raise KeyError(f"no profile for instance {inst.iid}; profile before scheduling")
    health = scheduler.health
    penalty = 0.0
    if health is not None and inst.iid in health.probation_set():
        penalty = health.penalty_s

    if request.phase is Phase.PREFILL:

        def price(r: Request) -> float:
            return prefill_seconds(profile, r) if warm else profile.prefill_time(r.input_len)

        resident = (
            resident_prefill_seconds(profile, inst)
            if warm
            else sum(price(r) for r in inst.prefill.values())
        )
        return (float(inst.decode_tokens()), resident + price(request) + penalty)

    correction = scheduler.monitor.decode_correction(inst.iid)
    headroom = profile.max_tokens(scheduler.slo.tpot_s / correction, len(inst.decode) + 1)
    return (
        float(inst.prefill_tokens()),
        float(inst.decode_tokens() + request.length) - headroom + penalty / scheduler.slo.tpot_s,
    )


def reference_decode_span(
    request: Request, start: float, token_s: float, estimate: Callable[[Request], int]
) -> ReferenceSpan:
    held = request.input_len + request.output_len
    expected = estimate(request)
    if expected > request.output_len:
        left = expected - request.output_len
    elif request.wanted_len > 0:
        left = max(0, request.wanted_len - request.output_len)
    else:
        return start, float("inf"), held
    return start, start + left * token_s, held + left


def reference_peak_holds(
    spans: Iterable[ReferenceSpan],
    start: float,
    end: float,
    *,
    held: int = 0,
    held_kv: float = 0.0,
) -> tuple[int, float]:
    events = sorted(
        (t, order, kv)
        for first, last, kv in spans
        if (first <= start or first < end) and last > start
        for t, order in ((max(first, start), 1), (min(last, end), 0))
    )
    peak, peak_kv = held, held_kv
    for _, order, kv in events:
        held += 1 if order else -1
        held_kv += kv if order else -kv
        if order:
            peak, peak_kv = max(peak, held), max(peak_kv, held_kv)
    return peak, peak_kv


@dataclass(frozen=True)
class ReferenceOccupancy:
    engines: tuple[Instance, ...]
    slots: int
    tokens: float
    step: float
    residents: tuple[ReferenceSpan, ...]
    queued: tuple[ReferenceSpan, ...]
    deadlines: tuple[float, ...]
    ends: dict[tuple[str, str], float]

    def schedule(
        self, slots: int, joins: tuple[ReferenceSpan, ...] = ()
    ) -> tuple[tuple[ReferenceSpan, ...], tuple[ReferenceSpan, ...]]:
        ends = sorted(last for _, last, _ in self.residents)
        free = [0.0] * (slots - len(ends)) + ends[-slots:] if slots > 0 else [math.inf]

        def take(hold: ReferenceSpan) -> ReferenceSpan:
            first, last, kv = hold
            start = max(first, free[0])
            end = start + (last - first)
            if slots > 0:
                heapq.heapreplace(free, end)
            return start, end, kv

        started: list[ReferenceSpan] = []
        joined: list[ReferenceSpan] = []
        for hold in self.queued:
            while len(joined) < len(joins) and joins[len(joined)][0] < hold[0]:
                joined.append(take(joins[len(joined)]))
            started.append(take(hold))
        joined += [take(hold) for hold in joins[len(joined) :]]
        return tuple(started), tuple(joined)


def reference_decode_occupancy(
    scheduler: GlobalScheduler,
    input_len: int,
    *,
    concurrency: int = 0,
    expected_output: Callable[[Request], int] | None = None,
) -> ReferenceOccupancy | None:
    engines = tuple(scheduler.live_instances(Role.DECODE))
    if not engines:
        return None
    estimate = expected_output or (lambda r: r.wanted_len)
    slots, tokens, steps = 0, 0.0, {}
    for inst in engines:
        profile = scheduler.profiles.get(inst.iid)
        if profile is None or profile.decode_max_requests is None:
            return None
        limit = profile.decode_max_requests
        limit = min(limit, concurrency) if concurrency > 0 else limit
        token_limit = profile.decode_token_limit
        slots += limit
        tokens += float("inf") if token_limit is None else token_limit
        context = (inst.decode_tokens() + input_len) / (len(inst.decode) + 1)
        batch = limit * context if token_limit is None else min(limit * context, token_limit)
        steps[inst.iid] = profile.token_interval(
            batch, limit
        ) * scheduler.monitor.decode_correction(inst.iid)
    step = sum(steps.values()) / len(steps)
    residents: list[ReferenceSpan] = []
    ends: dict[tuple[str, str], float] = {}
    for inst in engines:
        for rid, r in inst.decode.items():
            resident = reference_decode_span(r, 0.0, steps[inst.iid], estimate)
            residents.append(resident)
            ends[inst.iid, rid] = resident[1]
    handoffs = [(0.0, r) for r in scheduler.monitor.waiting.values() if r.phase is Phase.DECODE]
    prefilling: list[tuple[float, Request]] = []
    for inst in scheduler.monitor.instances.values():
        prefill_profile = scheduler.profiles.get(inst.iid)
        done = 0.0
        for r in inst.prefill.values():
            if prefill_profile is not None:
                done += prefill_seconds(prefill_profile, r)
            prefilling.append((done, r))
    handoffs += sorted(prefilling, key=lambda row: row[0])
    now = scheduler._clock()
    return ReferenceOccupancy(
        engines,
        slots,
        tokens,
        step,
        tuple(residents),
        tuple(reference_decode_span(r, ready, step, estimate) for ready, r in handoffs),
        tuple(
            scheduler.slo.ttft_s - (now - r.arrived_at if r.arrived_at is not None else 0.0)
            for _, r in handoffs
        ),
        ends,
    )


def reference_decode_admits(
    scheduler: GlobalScheduler,
    request: Request,
    *,
    ready_s: float = 0.0,
    ttft_s: float | None = None,
    ttft_margin: float = 0.0,
    concurrency: int = 0,
    expected_output: Callable[[Request], int] | None = None,
    trace: list[str] | None = None,
) -> bool:
    """Return the cb41445 verdict, appending the deciding check to `trace`."""
    trace = [] if trace is None else trace
    estimate = expected_output or (lambda r: r.wanted_len)
    occupancy = reference_decode_occupancy(
        scheduler, request.input_len, concurrency=concurrency, expected_output=estimate
    )
    if occupancy is None:
        trace.append("unmeasured")
        return True
    started, ((start, end, request_kv),) = occupancy.schedule(
        occupancy.slots, (reference_decode_span(request, ready_s, occupancy.step, estimate),)
    )
    wait = start - ready_s
    if wait > 0 and not scheduler.meets_slo(
        replace(request, phase=Phase.PREFILL),
        (0.0, (ready_s if ttft_s is None else ttft_s) + wait),
        ttft_margin=ttft_margin,
    ):
        trace.append("wait")
        return False
    end = start if end == math.inf else end
    peak, peak_kv = reference_peak_holds(
        (*occupancy.residents, *started), start, end, held=1, held_kv=request_kv
    )
    if peak > 1 and peak_kv > occupancy.tokens:
        trace.append("kv")
        return False
    decode = replace(request, phase=Phase.DECODE)
    generating = {
        inst.iid: {
            rid: r for rid, r in inst.decode.items() if occupancy.ends[inst.iid, rid] > start
        }
        for inst in occupancy.engines
    }
    if any(
        scheduler.meets_slo(
            decode, reference_cost(scheduler, decode, replace(inst, decode=generating[inst.iid]))
        )
        for inst in occupancy.engines
    ):
        every = any(
            scheduler.meets_slo(decode, reference_cost(scheduler, decode, inst))
            for inst in occupancy.engines
        )
        trace.append("every resident fits" if every else "generating residents fit")
        return True
    trace.append("idle")
    return not any(
        scheduler.meets_slo(
            decode, reference_cost(scheduler, decode, replace(inst, prefill={}, decode={}))
        )
        for inst in occupancy.engines
    )


def reference_project_prefill(
    scorer: SplitScorer,
    now: float,
    request: Request | None = None,
    *,
    additional_prefill: tuple[Instance, ...] = (),
) -> PrefillProjection | None:
    pool = [*scorer.scheduler.live_instances(Role.PREFILL), *additional_prefill]
    if not pool:
        return None
    profiles: dict[str, Profile] = {}
    for inst in pool:
        profile = scorer.scheduler.profiles.get(inst.iid)
        if profile is None:
            return None
        profiles[inst.iid] = profile

    waiting = [row for row in scorer.monitor.waiting.values() if row.phase is Phase.PREFILL]
    if request is not None and all(row.rid != request.rid for row in waiting):
        waiting.append(request)
    scorer.recheck(waiting)
    if not waiting:
        return None
    waiting.sort(key=lambda row: row.arrived_at if row.arrived_at is not None else now)

    loads: dict[str, float] = {}
    resident_prefill = 0.0
    for inst in pool:
        profile = profiles[inst.iid]
        resident = resident_prefill_seconds(profile, inst)
        penalty = (
            scorer.scheduler.health.penalty_s
            if scorer.scheduler.health is not None
            and inst.iid in scorer.scheduler.health.probation_set()
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
        ttft_slo_s=scorer.scheduler.slo.ttft_s,
        resident_prefill_s=resident_prefill,
        queued_prefill_s=queued_prefill,
        waiting_prefill=len(waiting),
    )


def reference_cheapest(
    scheduler: GlobalScheduler,
    request: Request,
    candidates: list[Instance],
    prices: dict[str, Cost],
) -> tuple[Instance, bool]:
    eligible = [i for i in candidates if scheduler.meets_slo(request, prices[i.iid])]
    return min(eligible or candidates, key=lambda i: (prices[i.iid], i.iid)), bool(eligible)


def reference_cache_placement(
    scheduler: GlobalScheduler, request: Request, chosen: Instance, candidates: list[Instance]
) -> dict[str, Any] | None:
    profile = scheduler.profiles.get(chosen.iid)
    if profile is None:
        return None
    cold = {i.iid: reference_cost(scheduler, request, i, warm=False) for i in candidates}
    return {
        "placed_iid": chosen.iid,
        "placed_cached_tokens": request.cached_tokens.get(chosen.iid, 0),
        "evidence_sequence": request.cache_sequences.get(chosen.iid),
        "predicted_prefill_s": prefill_seconds(profile, request),
        "cold_prefill_s": profile.prefill_time(request.input_len),
        "cold_choice_iid": reference_cheapest(scheduler, request, candidates, cold)[0].iid,
    }


def reference_schedule(
    scheduler: GlobalScheduler, request: Request, exclude: set[str] | None = None
) -> Instance:
    exclude = set(exclude or ())
    # A decode leg avoids its producer while the decode pool holds another engine.
    if request.phase is Phase.DECODE and request.prefill_instance is not None:
        pool = scheduler.role_pool(Role.DECODE, scheduler.live_instances(exclude=exclude))
        if any(i.iid != request.prefill_instance for i in pool):
            exclude.add(request.prefill_instance)
    instances = scheduler.live_instances(exclude=exclude)
    if not instances:
        raise RuntimeError("no schedulable instances")
    want = Role.PREFILL if request.phase is Phase.PREFILL else Role.DECODE
    candidates = scheduler.role_pool(want, instances)
    if not candidates:
        raise RuntimeError("no schedulable instances for the pinned roles")
    if request.phase is Phase.PREFILL:
        request.cache_placement = None
        scheduler.recheck_evidence(request, 0.0)
    prices = {i.iid: reference_cost(scheduler, request, i) for i in candidates}
    chosen, served = reference_cheapest(scheduler, request, candidates, prices)
    if not served:
        scheduler.unserved += 1
    if request.phase is Phase.PREFILL and request.cached_tokens:
        request.cache_placement = reference_cache_placement(scheduler, request, chosen, candidates)
    return chosen


# Fuzzed fleets.


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@dataclass
class Fleet:
    clock: Clock
    monitor: InstanceMonitor
    scheduler: GlobalScheduler
    demand: DemandModel
    scorer: SplitScorer
    iids: list[str]
    probation: set[str]
    unbounded_kv: bool


def evidence(rng: random.Random, iids: list[str], input_len: int) -> dict[str, int]:
    """Cached tokens for some engines, zero included, inside and outside the warm fit."""
    return {
        iid: rng.choice((0, rng.randint(1, max(1, input_len - 1))))
        for iid in rng.sample(iids, rng.randint(0, len(iids)))
    }


def fuzz_request(rng: random.Random, rid: str, iids: list[str], phase: Phase) -> Request:
    """A request whose hold is finite, past its estimate under a cap, or unbounded."""
    input_len = rng.choice((rng.randint(5, 120), rng.randint(1, 4000)))
    kind = rng.choice(("finite", "past estimate", "unbounded", "at cap"))
    wanted_len = 0 if kind == "unbounded" else rng.randint(1, 600)
    output_len = {
        "finite": rng.randint(0, max(0, wanted_len - 1)),
        "past estimate": rng.randint(wanted_len // 2, max(wanted_len // 2, wanted_len - 1)),
        "unbounded": rng.randint(0, 300),
        "at cap": wanted_len,
    }[kind]
    return Request(
        rid,
        input_len,
        phase=phase,
        output_len=output_len if phase is Phase.DECODE else 0,
        wanted_len=wanted_len,
        arrived_at=rng.choice((None, 1000.0 - rng.uniform(0.0, 4.0))),
        cached_tokens=evidence(rng, iids, input_len) if phase is Phase.PREFILL else {},
    )


def build_fleet(rng: random.Random, directory: Path) -> Fleet:
    clock = Clock()
    store = ProfileStore(directory / "profiles.json", load=False)
    monitor = InstanceMonitor(clock, store)
    iids = [f"e{index}" for index in range(rng.randint(2, 6))]
    for iid in iids:
        kv = rng.choice((3_000, 20_000, 100_000))
        changes = {
            "ttft_a": rng.choice((0.0, 1e-7, 1e-6)),
            "ttft_b": rng.uniform(1e-4, 3e-3),
            "ttft_c": rng.uniform(0.0, 0.05),
            "tpot_slope": rng.choice((0.0, 1e-7, 1e-6, 1e-5)),
            "tpot_intercept": rng.uniform(0.001, 0.03),
            "tpot_request_slope": rng.choice((0.0, 0.0005, 0.002)),
            "decode_max_requests": rng.randint(1, 6),
            "decode_max_kv_tokens": kv,
            "kv_capacity_tokens": kv * rng.choice((1, 2)),
        }
        store.put((warm if rng.random() < 0.5 else profile)(iid, **changes))
        monitor.add(Instance(iid, f"http://{iid}", rng.choice((Role.PREFILL, Role.DECODE))))
    corrections = {iid: rng.choice((1.0, rng.uniform(0.5, 2.0))) for iid in iids}
    monitor.decode_correction = corrections.__getitem__
    health = DriftTracker(clock=clock) if rng.random() < 0.8 else None
    scheduler = GlobalScheduler(
        monitor,
        store,
        SLO(rng.uniform(0.2, 3.0), rng.uniform(0.005, 0.1)),
        clock=clock,
        health=health,
        decode_concurrency=rng.choice((0, 1, 2, 3)),
    )
    demand = DemandModel(monitor, scheduler, clock, window_s=60.0, bucket_s=1.0)
    for _ in range(rng.randint(0, 30)):
        input_len, wanted_len = rng.choice((16, 100, 900)), rng.choice((0, 64, 256))
        demand.saw_completion(input_len, wanted_len, rng.randint(1, max(1, wanted_len or 300)))
    for iid in iids:
        for index in range(rng.choice((0, rng.randint(1, 9)))):
            monitor.dispatched(iid, fuzz_request(rng, f"{iid}-d{index}", iids, Phase.DECODE))
        for index in range(rng.choice((0, 0, rng.randint(1, 4)))):
            monitor.dispatched(iid, fuzz_request(rng, f"{iid}-p{index}", iids, Phase.PREFILL))
    for index in range(rng.randint(0, 6)):
        row = fuzz_request(rng, f"w{index}", iids, rng.choice((Phase.PREFILL, Phase.DECODE)))
        monitor.waiting[row.rid] = row
    if rng.random() < 0.2:
        scheduler.drain(rng.choice(iids))
    return Fleet(
        clock=clock,
        monitor=monitor,
        scheduler=scheduler,
        demand=demand,
        scorer=SplitScorer(demand),
        iids=iids,
        probation=set(rng.sample(iids, rng.randint(0, len(iids)))),
        unbounded_kv=rng.random() < 0.2,
    )


def estimators(
    fleet: Fleet,
) -> list[tuple[Callable[[Request], int] | None, Callable[[Request], int] | None]]:
    """Pairs of the current and cb41445 estimators for the same output estimate."""
    demand = fleet.demand
    _, estimates, median = demand._current_snapshot()
    return [
        (None, None),
        (lambda r: 0, lambda r: 0),
        (lambda r: r.wanted_len // 2, lambda r: r.wanted_len // 2),
        (lambda r: 150, lambda r: 150),
        (
            demand.output_estimator(),
            lambda r: demand._expected_output(r.input_len, r.wanted_len, estimates) or median,
        ),
    ]


def spans(rng: random.Random, count: int) -> tuple[ReferenceSpan, ...]:
    """Holds in handoff order, some unbounded and some zero-length."""
    rows = []
    for _ in range(count):
        first = rng.choice((0.0, rng.uniform(0.0, 5.0)))
        length = rng.choice((0.0, math.inf, rng.uniform(0.0, 10.0)))
        rows.append((first, first + length, rng.randint(0, 5_000)))
    return tuple(sorted(rows, key=lambda row: row[0]))


class AdmissionReferenceTests(unittest.TestCase):
    """Admission, projection and placement give the cb41445 results on fuzzed fleets."""

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.seen: Counter[str] = Counter()

    def fleets(self, count: int, seed: int) -> Iterator[Fleet]:
        """Yield fuzzed fleets with their probation set and, for some, unbounded decode KV."""
        rng = random.Random(seed)
        for index in range(count):
            directory = self.root / f"{seed}-{index}"
            directory.mkdir()
            fleet = build_fleet(rng, directory)
            with ExitStack() as stack:
                if fleet.scheduler.health is not None:
                    stack.enter_context(
                        patch.object(
                            fleet.scheduler.health, "probation_set", return_value=fleet.probation
                        )
                    )
                if fleet.unbounded_kv:
                    # Profile validation requires KV bounds; unbounded decode KV skips it.
                    stack.enter_context(
                        patch.object(Profile, "decode_token_limit", property(lambda self: None))
                    )
                yield fleet

    def test_decode_occupancy_and_schedule_match_the_reference(self):
        rng = random.Random(11)
        for fleet in self.fleets(250, seed=1):
            for current, reference in estimators(fleet):
                input_len = rng.randint(1, 4000)
                concurrency = rng.choice((0, 1, 2, 3, 8))
                new = decode_occupancy(
                    fleet.scheduler, input_len, concurrency=concurrency, expected_output=current
                )
                old = reference_decode_occupancy(
                    fleet.scheduler, input_len, concurrency=concurrency, expected_output=reference
                )
                if old is None:
                    self.assertIsNone(new)
                    continue
                assert new is not None
                self.assertEqual(
                    (new.engines, new.slots, new.tokens, new.step, new.residents),
                    (old.engines, old.slots, old.tokens, old.step, old.residents),
                )
                self.assertEqual(
                    (new.queued, new.deadlines, new.ends), (old.queued, old.deadlines, old.ends)
                )
                self.assertEqual(new.held, {i.iid: i.decode_tokens() for i in new.engines})
                for slots in range(new.slots + 3):
                    joins = spans(rng, rng.randint(0, 3))
                    self.assertEqual(new.schedule(slots, joins), old.schedule(slots, joins))
                    self.assertEqual(new.schedule(slots), old.schedule(slots))
                    holds = len(new.queued) + len(joins)
                    self.seen[
                        "residents above slots" if len(new.residents) > slots else "below"
                    ] += 1
                    self.seen[
                        "holds take resident ends" if slots - len(new.residents) < holds else "idle"
                    ] += 1
        self.assertTrue(
            all(
                self.seen[key]
                for key in ("residents above slots", "below", "holds take resident ends", "idle")
            ),
            self.seen,
        )

    def test_peak_holds_match_the_reference(self):
        rng = random.Random(5)
        for _ in range(2_000):
            rows = spans(rng, rng.randint(0, 12))
            start = rng.uniform(0.0, 6.0)
            end = rng.choice((start, start + rng.uniform(0.0, 8.0)))
            held, held_kv = rng.randint(0, 2), rng.randint(0, 3_000)
            self.assertEqual(
                peak_holds(rows, start, end, held=held, held_kv=held_kv),
                reference_peak_holds(rows, start, end, held=held, held_kv=held_kv),
            )

    def test_decode_admits_matches_the_reference(self):
        rng = random.Random(13)
        holds: Counter[str] = Counter()
        for fleet in self.fleets(300, seed=2):
            for current, reference in estimators(fleet):
                for index in range(4):
                    request = fuzz_request(rng, f"new{index}", fleet.iids, Phase.PREFILL)
                    request.cached_tokens = {}
                    options = {
                        "ready_s": rng.choice((0.0, rng.uniform(0.0, 3.0))),
                        "ttft_s": rng.choice((None, rng.uniform(0.0, 3.0))),
                        "ttft_margin": rng.choice((0.0, 0.2)),
                        "concurrency": rng.choice((0, 1, 2, 3)),
                    }
                    trace: list[str] = []
                    expected = reference_decode_admits(
                        fleet.scheduler, request, expected_output=reference, trace=trace, **options
                    )
                    self.assertEqual(
                        decode_admits(fleet.scheduler, request, expected_output=current, **options),
                        expected,
                    )
                    self.seen[trace[-1]] += 1
                    estimate = current or (lambda r: r.wanted_len)
                    _, last, _ = decode_span(request, 0.0, 1.0, estimate)
                    holds["unbounded" if last == math.inf else "bounded"] += 1
                    if fleet.unbounded_kv:
                        self.seen["unbounded kv"] += 1
        for check in (
            "unmeasured",
            "wait",
            "kv",
            "every resident fits",
            "generating residents fit",
            "idle",
            "unbounded kv",
        ):
            self.assertTrue(self.seen[check], (check, self.seen))
        self.assertTrue(holds["unbounded"] and holds["bounded"], holds)

    def test_project_prefill_matches_the_reference(self):
        rng = random.Random(17)
        for fleet in self.fleets(250, seed=3):
            waiting = list(fleet.monitor.waiting.values())
            decode = [i for i in fleet.monitor.instances.values() if i.role is Role.DECODE]
            for _ in range(4):
                request = rng.choice(
                    (
                        None,
                        fuzz_request(rng, "offer", fleet.iids, Phase.PREFILL),
                        *waiting,
                        *(replace(row, phase=Phase.PREFILL) for row in waiting),
                    )
                )
                additional = tuple(rng.sample(decode, rng.randint(0, len(decode))))
                now = fleet.clock.now + rng.choice((0.0, rng.uniform(0.0, 2.0)))
                self.assertEqual(
                    fleet.scorer.project_prefill(now, request, additional_prefill=additional),
                    reference_project_prefill(
                        fleet.scorer, now, request, additional_prefill=additional
                    ),
                )

    def test_placement_matches_the_reference(self):
        rng = random.Random(19)
        placed: Counter[str] = Counter()
        for fleet in self.fleets(300, seed=4):
            scheduler = fleet.scheduler
            for index in range(6):
                phase = rng.choice((Phase.PREFILL, Phase.PREFILL, Phase.DECODE))
                request = fuzz_request(rng, f"q{index}", fleet.iids, phase)
                if phase is Phase.DECODE:
                    request.prefill_instance = rng.choice((None, *fleet.iids))
                exclude = set(rng.sample(fleet.iids, rng.choice((0, 0, 1, len(fleet.iids)))))
                outcomes = []
                for place in (reference_schedule, GlobalScheduler.schedule):
                    row = copy.deepcopy(request)
                    unserved = scheduler.unserved
                    try:
                        chosen = place(scheduler, row, set(exclude)).iid
                    except RuntimeError as exc:
                        chosen = str(exc)
                    outcomes.append((chosen, row.cache_placement, scheduler.unserved - unserved))
                self.assertEqual(outcomes[1], outcomes[0])
                record = outcomes[0][1]
                if record is not None:
                    placed[
                        "cold choice moves"
                        if record["cold_choice_iid"] != record["placed_iid"]
                        else "cold choice stays"
                    ] += 1
        self.assertTrue(placed["cold choice moves"] and placed["cold choice stays"], placed)

    def test_placement_reads_probation_once(self):
        checked = 0
        for fleet in self.fleets(80, seed=5):
            scheduler = fleet.scheduler
            if scheduler.health is None:
                continue
            prefill = scheduler.role_pool(Role.PREFILL, scheduler.live_instances())
            request = Request("q", 40, cached_tokens={i.iid: 32 for i in prefill})
            with patch.object(
                scheduler.health, "probation_set", return_value=fleet.probation
            ) as probation:
                scheduler.schedule(request)
            self.assertEqual(probation.call_count, 1)
            self.assertIsNotNone(request.cache_placement)
            checked += 1 if len(prefill) > 1 else 0
        self.assertTrue(checked)

    def test_output_estimates_follow_their_snapshot(self):
        rng = random.Random(29)
        clock = Clock()
        store = ProfileStore(self.root / "profiles.json", load=False)
        monitor = InstanceMonitor(clock, store)
        scheduler = GlobalScheduler(monitor, store, SLO(1.0, 0.05), clock=clock)
        demand = DemandModel(monitor, scheduler, clock, window_s=60.0, bucket_s=1.0)
        shapes = [(rng.choice((16, 100, 900, 5000)), rng.choice((0, 32, 256))) for _ in range(200)]
        for _ in range(3):
            for input_len, wanted_len in rng.sample(shapes, 20):
                observed = rng.randint(1, max(1, wanted_len or 400))
                demand.saw_completion(input_len, wanted_len, observed)
            estimate = demand.output_estimator()
            _, estimates, median = demand._current_snapshot()
            for input_len, wanted_len in shapes * 2:
                request = Request("r", input_len, wanted_len=wanted_len)
                self.assertEqual(
                    estimate(request),
                    demand._expected_output(input_len, wanted_len, estimates) or median,
                )
            clock.now += demand.window_s


if __name__ == "__main__":
    unittest.main()
