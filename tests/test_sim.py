"""The simulator behind the table `make demo` prints, and §6.1's attainment metric.

The published table is the only result this project reports, so the golden run
at the end pins it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from narwhal.monitor import InstanceMonitor
from narwhal.profiler import Profile, ProfileStore
from narwhal.scheduler import SLO, GlobalScheduler, Thresholds
from narwhal.sim import Fleet, TraceEntry, _Live
from narwhal.types import Instance, Phase, Request, Role

TTFT_SLO = 10.0
TPOT_SLO = 0.125


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _fleet(tmp_path: Path, clock=None, n: int = 1) -> Fleet:
    clock = clock or _Clock()
    mon = InstanceMonitor(clock=clock)
    store = ProfileStore(tmp_path / "profiles.json")
    for k in range(n):
        mon.add(Instance(iid=f"i{k}", url=f"http://i{k}", role=Role.PREFILL))
        store.put(
            Profile(
                iid=f"i{k}",
                ttft_a=2e-8,
                ttft_b=6e-5,
                ttft_c=0.005,
                tpot_slope=3e-6,
                tpot_intercept=0.012,
            )
        )
    return Fleet(mon, store, clock, kv_transfer_s=0.05, dt=0.01)


def _live(rid: str, *, first_token_at, token_times, finished_at, output_len: int = 4) -> _Live:
    return _Live(
        entry=TraceEntry(at=0.0, rid=rid, input_len=100, output_len=output_len),
        request=Request(rid=rid, input_len=100),
        arrived=0.0,
        first_token_at=first_token_at,
        tokens=len(token_times),
        finished_at=finished_at,
        token_times=list(token_times),
    )


def _met(rid: str) -> _Live:
    """Finished, first token at 1 s, tokens 0.1 s apart."""
    return _live(rid, first_token_at=1.0, token_times=[1.0, 1.1, 1.2, 1.3], finished_at=1.3)


# -- the metric (§6.1) ----------------------------------------------------


def test_the_denominator_is_what_was_offered(tmp_path):
    """A request the fleet never finished counts against it.

    Scoring completions instead rewards dropping load: a 1P7D split read 100%
    while finishing 35% of the trace.
    """
    fleet = _fleet(tmp_path)
    fleet.live = {
        "a": _met("a"),
        "b": _met("b"),
        "c": _live("c", first_token_at=1.0, token_times=[1.0, 1.1], finished_at=None),
        "d": _live("d", first_token_at=None, token_times=[], finished_at=None),
    }

    frac, met, total = fleet.attainment(TTFT_SLO, TPOT_SLO)
    assert (frac, met, total) == (0.5, 2, 4)
    assert total == len(fleet.live), "every admitted request is in the denominator"

    finished = [live for live in fleet.live.values() if live.finished_at is not None]
    assert met / len(finished) == 1.0, "a finished-only denominator would read 100%"


def test_an_empty_fleet_scores_zero(tmp_path):
    assert _fleet(tmp_path).attainment(TTFT_SLO, TPOT_SLO) == (0.0, 0, 0)


def test_a_ttft_overrun_misses(tmp_path):
    fleet = _fleet(tmp_path)
    fleet.live = {"a": _live("a", first_token_at=20.0, token_times=[20.0, 20.1], finished_at=20.1)}
    assert fleet.attainment(TTFT_SLO, TPOT_SLO) == (0.0, 0, 1)


def test_a_tpot_overrun_misses_on_a_prompt_that_started_fast(tmp_path):
    """One second between tokens against a 125 ms target, with TTFT met."""
    fleet = _fleet(tmp_path)
    fleet.live = {"a": _live("a", first_token_at=1.0, token_times=[1.0, 2.0, 3.0], finished_at=3.0)}
    assert fleet.attainment(TTFT_SLO, TPOT_SLO) == (0.0, 0, 1)


def test_a_single_token_response_has_no_interval_to_miss(tmp_path):
    """Arrow §4.3 divides by `m - 1`, so one token has no TPOT. `bench.score` agrees."""
    fleet = _fleet(tmp_path)
    live = _live("a", first_token_at=1.0, token_times=[1.0], finished_at=1.0, output_len=1)
    assert live.tpot is None
    fleet.live = {"a": live}
    assert fleet.attainment(TTFT_SLO, TPOT_SLO) == (1.0, 1, 1)


def test_tpot_is_the_mean_gap_over_m_minus_one(tmp_path):
    """Arrow §4.3's `sum(t_j) / (m - 1)`, not the span over the token count."""
    live = _live("a", first_token_at=1.0, token_times=[1.0, 1.5, 2.5], finished_at=2.5)
    assert live.ttft == 1.0
    assert live.tpot == 0.75


# -- what the simulator does per step -------------------------------------


def test_a_request_finishes_only_when_every_token_landed(tmp_path):
    """`finished_at` is set at `tokens >= output_len`, so a short answer never
    reaches the numerator."""
    clock = _Clock()
    fleet = _fleet(tmp_path, clock)
    entry = TraceEntry(at=0.0, rid="r1", input_len=100, output_len=3)
    fleet.admit(entry, "i0")

    progress: list[tuple[int, float | None]] = []
    for _ in range(500):
        for rid in list(fleet.awaiting_decode):
            fleet.awaiting_decode.remove(rid)
            fleet.dispatch_decode(rid, "i0")
        fleet.step()
        live = fleet.live["r1"]
        progress.append((live.tokens, live.finished_at))
        if live.finished_at is not None:
            break
        clock.t += 0.01

    assert all(done is None for tokens, done in progress if tokens < 3)
    assert progress[-1][0] == 3
    assert fleet.attainment(TTFT_SLO, TPOT_SLO) == (1.0, 1, 1)


def test_a_decode_leg_that_crosses_instances_waits_for_the_kv_transfer(tmp_path):
    """Arrow §3.1's transfer is charged when the decode instance differs from the
    prefill instance, and only then."""
    clock = _Clock()
    fleet = _fleet(tmp_path, clock, n=2)
    fleet.admit(TraceEntry(at=0.0, rid="r1", input_len=100, output_len=3), "i0")
    fleet.admit(TraceEntry(at=0.0, rid="r2", input_len=100, output_len=3), "i0")

    fleet.dispatch_decode("r1", "i1")
    assert list(fleet.local["i1"].migration) == [("r1", 0.05)]
    assert fleet.local["i1"].decode_ready == []

    fleet.dispatch_decode("r2", "i0")
    assert list(fleet.local["i0"].migration) == []
    assert fleet.local["i0"].decode_ready == ["r2"]

    clock.t = 0.05
    assert fleet.local["i1"].release_migrations(clock.t) == ["r1"]


# -- the published table --------------------------------------------------

# demo/run_demo.py --rates 2.4,6.4 at seed 7, the 2.4 and 6.4 columns of the
# `make demo` table. The three cells a finished-only denominator moves are here:
# static 1P7D at 6.4, static 7P1D at 2.4 and the adaptive arm at 6.4.
GOLDEN = [
    "static 1P7D             39%    33%        none",
    "static 2P6D             67%    33%        none",
    "static 3P5D             98%    33%   2.4 req/s",
    "static 4P4D            100%    33%   2.4 req/s",
    "static 5P3D             71%    31%        none",
    "static 6P2D             48%    19%        none",
    "static 7P1D             27%     7%        none",
    "aggregated 8x           98%    30%   2.4 req/s",
    "arrow (adaptive)       100%    54%   2.4 req/s",
]


def test_the_published_table_reproduces_at_two_rates():
    """Roughly 20 s. The whole sweep is 8 rates and takes 80."""
    demo = Path(__file__).resolve().parents[1] / "demo" / "run_demo.py"
    proc = subprocess.run(
        [sys.executable, str(demo), "--rates", "2.4,6.4"],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    rows = [
        line
        for line in proc.stdout.splitlines()
        if line.startswith(("static ", "aggregated ", "arrow ("))
    ]
    assert rows == GOLDEN


# -- re-placement of queued prefill legs --------------------------------


def _drive_skew(fleet, sched, clock, entries, rebalance: bool) -> None:
    """Every arrival queues at i0 - the skew itself - and decode stays where
    the prefill landed. The rebalanced arm runs one pass per half second."""
    pending = sorted(entries, key=lambda e: e.at)
    idx, next_pass = 0, 0.0
    while clock.t < 200.0:
        while idx < len(pending) and pending[idx].at <= clock.t:
            fleet.admit(pending[idx], "i0")
            idx += 1
        for rid in list(fleet.awaiting_decode):
            fleet.awaiting_decode.remove(rid)
            fleet.dispatch_decode(rid, fleet.live[rid].request.prefill_instance)
        if rebalance and clock.t >= next_pass:
            applied = 0
            for rid, src, dst in sched.queue_replacements(slack_s=0.5):
                if applied >= 2:
                    break
                req = fleet.monitor.instances[src].prefill.get(rid)
                if req is None:
                    continue
                # A leg cancelled mid-prefill re-runs whole: any partial chunk
                # the source computed is honestly paid for and discarded.
                fleet.monitor.finished(src, rid)
                fleet.monitor.dispatched(dst, req)
                fleet.local[src].drop(rid)
                fleet.local[dst].admit_prefill(rid, req.input_len)
                req.replaced += 1
                applied += 1
            next_pass = clock.t + 0.5
        fleet.step()
        clock.t += 0.01
        if idx >= len(pending) and all(
            live.finished_at is not None for live in fleet.live.values()
        ):
            break


def test_re_placement_turns_a_queue_death_row_into_a_clean_run(tmp_path):
    """Three 12k-token prefills land on one engine while its peer idles. The
    staying price misses TTFT for all three; moves of two per pass spread
    the queue while both engines still hold their SLO on the other side."""
    from narwhal.scheduler import SLO, GlobalScheduler, Thresholds

    entries = [TraceEntry(at=0.0, rid=f"r{k}", input_len=12_000, output_len=20) for k in range(3)]
    arms = {}
    for name, rebalance in (("life-sentence", False), ("replaced", True)):
        clock = _Clock()
        fleet = _fleet(tmp_path / name, clock, n=2)
        sched = GlobalScheduler(
            fleet.monitor,
            fleet.profiles,
            SLO(ttft_s=TTFT_SLO, tpot_s=TPOT_SLO),
            Thresholds(),
            clock=clock,
        )
        _drive_skew(fleet, sched, clock, entries, rebalance)
        arms[name] = fleet.attainment(TTFT_SLO, TPOT_SLO)

    assert arms["life-sentence"] == (0.0, 0, 3), "all three die queued without the pass"
    assert arms["replaced"] == (1.0, 3, 3), "every leg serves inside SLO once re-placed"


# -- paired arms on a shared-prefix flood -----------------------------


def _prefix_flood_arm(
    tmp_path: Path,
    *,
    coop: bool,
    affinity: bool,
    rate: float = 3.0,
    keys: int = 16,
    hot_key: int | None = None,
) -> tuple[float, int, int]:
    """One arm of the cooperative-reuse pairing, demand in two shapes.

     `hot_key` set: every arrival shares one prefix - the shared-prefix
     trace, RAG's own shape. unset: `keys` prefixes cycle, each engine's LRU
     holding 8, so reuse exists only where placement remembers. All requests
     carry a 4800-token shared head under ~6500 tokens, 24 seconds of arrivals
     against two prefill engines. Role movement is owned away: this measures
     the placement term, and the control loops have their own ablations
    .
    """
    import random

    clock = _Clock()
    mon = InstanceMonitor(clock=clock)
    store = ProfileStore(tmp_path / f"profiles-{coop}-{affinity}-{rate}-{keys}-{hot_key}.json")
    for k in range(4):
        iid = f"i{k}"
        mon.add(Instance(iid=iid, url=f"http://{iid}", role=Role.PREFILL if k < 2 else Role.DECODE))
        store.put(
            Profile(
                iid=iid,
                ttft_a=2e-8,
                ttft_b=6e-5,
                ttft_c=0.005,
                tpot_slope=3e-6,
                tpot_intercept=0.012,
            )
        )
    sched = GlobalScheduler(
        mon,
        store,
        SLO(ttft_s=TTFT_SLO, tpot_s=TPOT_SLO),
        Thresholds(),
        clock=clock,
        prefill_affinity=affinity,
        prefix_coop=coop,
    )
    # The pin is the term, not the control loops: step 3 falls back to the
    # cheapest candidate and the three arms differ only in placement.
    sched.controller_owns_flips = True
    fleet = Fleet(mon, store, clock, kv_transfer_s=0.05, dt=0.01, cache_keys=8)

    rng = random.Random(11)
    entries = []
    t = 0.0
    k = 0
    while t < 24.0:
        t += rng.expovariate(rate)
        if t >= 24.0:
            break
        key = hot_key if hot_key is not None else k % keys
        entries.append(
            TraceEntry(
                at=t,
                rid=f"r{k}",
                input_len=rng.randint(6250, 6750),
                output_len=4,
                prefix_key=key,
                prefix_len=4800,
            )
        )
        k += 1

    idx, next_pass = 0, 0.0
    horizon = entries[-1].at + 90.0
    while clock.t < horizon:
        while idx < len(entries) and entries[idx].at <= clock.t:
            e = entries[idx]
            idx += 1
            r = Request(
                rid=e.rid,
                input_len=e.input_len,
                phase=Phase.PREFILL,
                prefix_key=e.prefix_key,
                prefix_len=e.prefix_len,
            )
            target = sched.schedule(r)
            # Teaching moved to dispatch (a refused request must not warm
            # the map); the drive admits everything, so it teaches here.
            sched.remember(r, target)
            fleet.admit(e, target.iid)
        for rid in list(fleet.awaiting_decode):
            fleet.awaiting_decode.remove(rid)
            live = fleet.live[rid]
            live.request.phase = Phase.DECODE
            fleet.dispatch_decode(rid, sched.schedule(live.request).iid)
        fleet.step()
        if clock.t >= next_pass:
            sched.monitoring_pass()
            next_pass = clock.t + 1.0
        clock.t += 0.01
        if idx >= len(entries) and all(v.finished_at for v in fleet.live.values()):
            break
    return fleet.attainment(TTFT_SLO, TPOT_SLO)


def test_pricing_reuse_beats_ignoring_it_where_caches_bind(tmp_path):
    """Coop against off, sixteen prefixes against two 8-deep caches: the
    fleet's reuse is real only for a router that remembers placement."""
    off3 = _prefix_flood_arm(tmp_path, coop=False, affinity=False, rate=3.0)
    coop3 = _prefix_flood_arm(tmp_path, coop=True, affinity=False, rate=3.0)
    off4 = _prefix_flood_arm(tmp_path, coop=False, affinity=False, rate=4.0)
    coop4 = _prefix_flood_arm(tmp_path, coop=True, affinity=False, rate=4.0)
    assert coop3[1:] == (62, 79)
    assert coop3[0] > off3[0], (coop3, off3)
    assert coop4[1:] == (20, 109)
    assert coop4[0] > off4[0], (coop4, off4)


def test_pricing_reuse_survives_the_pile_that_kills_blind_affinity(tmp_path):
    """Coop against affinity, one hot prefix past one engine's rate: the
    blind arm stacks its warm engine deeper without reading the queue; the
    priced arm sheds overflow at full prefill and keeps both engines warm."""
    aff4 = _prefix_flood_arm(tmp_path, coop=False, affinity=True, rate=4.0, hot_key=42)
    coop4 = _prefix_flood_arm(tmp_path, coop=True, affinity=False, rate=4.0, hot_key=42)
    assert coop4[0] > aff4[0], (coop4, aff4)
    assert aff4[1:] == (9, 109)


def test_pricing_reuse_costs_nothing_where_balance_already_reuses(tmp_path):
    """At a rate both engines answer, one warm prefix needs no steering:
    coop's pile margin must not spend attainment plain balancing was
    getting. The sim says the term costs under six points there."""
    off3 = _prefix_flood_arm(tmp_path, coop=False, affinity=False, rate=3.0, hot_key=42)
    coop3 = _prefix_flood_arm(tmp_path, coop=True, affinity=False, rate=3.0, hot_key=42)
    assert off3[1:] == (79, 79)
    assert coop3[0] >= off3[0] - 0.06, (coop3, off3)
