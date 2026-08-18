"""Each paper-derived test names the clause of arXiv:2505.11916 it holds the code to.

The extension tests stand on their own names.
"""

from __future__ import annotations

from collections import deque

import pytest

from narwhal.monitor import InstanceMonitor
from narwhal.profiler import Profile, ProfileStore, fit_linear, fit_quadratic
from narwhal.scheduler import SLO, GlobalScheduler, Thresholds
from narwhal.types import Instance, Phase, Request, Role


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def build(n_prefill: int = 2, n_decode: int = 2, tmp_path=None):
    clock = FakeClock()
    store = ProfileStore(tmp_path / "profiles.json")
    mon = InstanceMonitor(clock=clock, profiles=store)
    for k in range(n_prefill + n_decode):
        iid = f"i{k}"
        role = Role.PREFILL if k < n_prefill else Role.DECODE
        mon.add(Instance(iid=iid, url=f"http://{iid}", role=role))
        # 1 ms per token linear plus a small quadratic term, and a decode
        # ceiling of 4000 tokens at a 50 ms TPOT target.
        store.put(
            Profile(
                iid=iid,
                ttft_a=1e-8,
                ttft_b=1e-3,
                ttft_c=0.0,
                tpot_slope=1.25e-5,
                tpot_intercept=0.0,
            )
        )
    sched = GlobalScheduler(mon, store, SLO(ttft_s=1.0, tpot_s=0.05), Thresholds(), clock=clock)
    # The fixture fleet has been up for a full cooldown: tests exercise the
    # steady state unless they spend the cooldown themselves.
    sched._last_p2d_flip -= sched.th.cooldown_s
    return clock, mon, sched


# -- Arrow §5.2 stateless instances ------------------------------------------


def test_role_is_a_label_not_a_capability(tmp_path):
    """Arrow §5.2: prefill and decode are "solely as attributes of requests".

    Flipping mutates a label and nothing else. Nothing drains, nothing
    restarts, and the requests already resident are untouched.
    """
    _, mon, sched = build(tmp_path=tmp_path)
    for inst in mon.pool(Role.PREFILL):
        mon.dispatched(inst.iid, Request(rid=f"r{inst.iid}", input_len=100))

    flipped = sched.flip(Role.DECODE)

    assert flipped is not None
    assert flipped.role is Role.DECODE
    assert flipped.prefill, "flipping must not evict resident work"


def test_a_flip_never_empties_the_source_pool(tmp_path):
    """Algorithm 3 guards on `|S| > 1`."""
    _, mon, sched = build(n_prefill=1, n_decode=3, tmp_path=tmp_path)
    assert sched.flip(Role.DECODE) is None
    assert len(mon.pool(Role.PREFILL)) == 1


# -- §5.3 cost functions ------------------------------------------------


def test_prefill_cost_prefers_an_instance_free_of_decode_work(tmp_path):
    """§5.3: the first component "encourages the scheduler to favor instances
    that are only handling prefill requests over others"."""
    _, mon, sched = build(tmp_path=tmp_path)
    clean, busy = mon.pool(Role.PREFILL)
    mon.dispatched(busy.iid, Request(rid="d0", input_len=500, phase=Phase.DECODE))

    r = Request(rid="r1", input_len=100)
    assert sched.cost(r, clean)[0] == 0.0
    assert sched.cost(r, busy)[0] == 500.0
    assert sched.schedule(r).iid == clean.iid


def test_decode_cost_is_distance_past_the_instances_own_tpot_ceiling(tmp_path):
    """§5.3 subtracts `MT(i, SLO_TPOT)`, so the sign is the SLO verdict."""
    _, mon, sched = build(tmp_path=tmp_path)
    inst = mon.pool(Role.DECODE)[0]
    ceiling = sched.profiles.get(inst.iid).max_tokens(sched.slo.tpot_s)
    assert ceiling == pytest.approx(4000.0)

    small = Request(rid="d1", input_len=10, phase=Phase.DECODE)
    assert sched.cost(small, inst)[1] == pytest.approx(10.0 - 4000.0)
    assert sched.meets_slo(small, sched.cost(small, inst))

    mon.dispatched(inst.iid, Request(rid="d2", input_len=4200, phase=Phase.DECODE))
    assert not sched.meets_slo(small, sched.cost(small, inst))


def test_the_slo_check_reads_the_second_component_only(tmp_path):
    """§5.3: "checks whether the second component of the prefill cost exceeds
    the TTFT SLO threshold"."""
    _, mon, sched = build(tmp_path=tmp_path)
    inst = mon.pool(Role.PREFILL)[0]
    assert sched.meets_slo(Request(rid="a", input_len=500), (9e9, 0.5))
    assert not sched.meets_slo(Request(rid="b", input_len=500), (0.0, 1.5))
    del inst


# -- §5.3 Algorithm 1 ---------------------------------------------------


def test_a_decode_request_returns_to_its_prefill_instance_once_that_flipped(tmp_path):
    """Algorithm 1, line 3: skip the KV transfer entirely."""
    _, mon, sched = build(tmp_path=tmp_path)
    origin = mon.pool(Role.PREFILL)[0]
    r = Request(rid="r2", input_len=100)
    mon.dispatched(origin.iid, r)
    mon.first_token(origin.iid, r.rid)

    origin.role = Role.DECODE
    r.phase = Phase.DECODE
    assert sched.schedule(r).iid == origin.iid


def test_an_unservable_prefill_request_flips_a_decode_instance(tmp_path):
    """Algorithm 1, line 13: flip rather than queue behind a violated SLO."""
    _, mon, sched = build(n_prefill=1, n_decode=3, tmp_path=tmp_path)
    # Load every instance past the TTFT target so nothing is eligible.
    for inst in mon.instances.values():
        mon.dispatched(inst.iid, Request(rid=f"p{inst.iid}", input_len=2000))

    before = len(mon.pool(Role.PREFILL))
    chosen = sched.schedule(Request(rid="r3", input_len=2000))
    assert len(mon.pool(Role.PREFILL)) == before + 1
    assert chosen.role is Role.PREFILL


def test_a_loaded_decode_pool_refuses_to_give_up_an_instance(tmp_path):
    """Arrow §5.5's overload rule: a D->P flip aborts while decode load is high, "to
    avoid scenarios where a large number of requests occupy memory resources
    without progressing beyond the prefill phase"."""
    clock, mon, sched = build(n_prefill=1, n_decode=3, tmp_path=tmp_path)
    for inst in mon.instances.values():
        mon.dispatched(inst.iid, Request(rid=f"p{inst.iid}", input_len=2000))
    # Drive decode load over `expand` by making token intervals miss TPOT.
    for inst in mon.pool(Role.DECODE):
        r = Request(rid=f"d{inst.iid}", input_len=10, phase=Phase.DECODE)
        mon.dispatched(inst.iid, r)
        mon.first_token(inst.iid, r.rid)
        clock.advance(0.2)  # 200 ms against a 50 ms target
        mon.output_token(inst.iid, r.rid)

    assert sched.pool_load(Role.DECODE) >= sched.th.expand
    before = len(mon.pool(Role.DECODE))
    sched.schedule(Request(rid="r4", input_len=2000))
    assert len(mon.pool(Role.DECODE)) == before, "decode must keep its instances under load"


# -- Arrow §5.5 Algorithms 2 and 3 -------------------------------------------


def test_the_cooldown_is_one_sided(tmp_path):
    """Arrow §5.5: the cooldown "is only applied to P->D process ... In contrast,
    TTFT, due to its strong predictability and sensitivity to traffic spikes,
    requires rapid instance scheduling"."""
    clock, _, sched = build(n_prefill=3, n_decode=3, tmp_path=tmp_path)

    assert sched.flip(Role.DECODE) is not None
    assert sched.flip(Role.DECODE) is None, "a second P->D inside the cooldown must be refused"

    # The other direction is not gated at all, in the same instant.
    assert sched.flip(Role.PREFILL) is not None
    assert sched.flip(Role.PREFILL) is not None

    clock.advance(sched.th.cooldown_s)
    assert sched.flip(Role.DECODE) is not None


def test_an_incompletely_flipped_instance_is_flipped_back_first(tmp_path):
    """Arrow §5.5: an instance still holding the other type "indicates that the
    instance's role has been flipped previously and the flipping is not yet
    complete. The scheduler prioritizes flipping instances of this type"."""
    _, mon, sched = build(n_prefill=3, n_decode=1, tmp_path=tmp_path)
    a, b, c = mon.pool(Role.PREFILL)
    # `a` was flipped P->D->P recently and still holds decode work.
    mon.dispatched(a.iid, Request(rid="d9", input_len=10, phase=Phase.DECODE))
    # `b` is idle, `c` holds a little prefill: both would be cheaper on the
    # second component, and both must lose to `a` on the first.
    mon.dispatched(c.iid, Request(rid="p9", input_len=1))

    assert sched.flip(Role.DECODE).iid == a.iid
    del b


def test_the_monitoring_loop_expands_decode_when_it_misses_tpot(tmp_path):
    """Algorithm 2: `LD >= L_EXPAND` flips P->D."""
    clock, mon, sched = build(n_prefill=2, n_decode=2, tmp_path=tmp_path)
    for inst in mon.pool(Role.DECODE):
        r = Request(rid=f"d{inst.iid}", input_len=10, phase=Phase.DECODE)
        mon.dispatched(inst.iid, r)
        mon.first_token(inst.iid, r.rid)
        clock.advance(0.1)
        mon.output_token(inst.iid, r.rid)

    for _ in range(sched.th.sustained_intervals - 1):
        assert sched.monitoring_pass() is None, "one crossing is not a period of time"
    flipped = sched.monitoring_pass()
    assert flipped is not None
    assert flipped.role is Role.DECODE
    assert len(mon.pool(Role.DECODE)) == 3


def test_an_idle_prefill_pool_is_lent_to_decode(tmp_path):
    """Algorithm 2's second trigger, `LP <= L_SHRINK <= LD`: idle prefill
    instances join decode "to free up computing resources as quickly as
    possible in anticipation of potential future bursty traffic"."""
    clock, mon, sched = build(n_prefill=2, n_decode=2, tmp_path=tmp_path)
    inst = mon.pool(Role.DECODE)[0]
    r = Request(rid="d5", input_len=10, phase=Phase.DECODE)
    mon.dispatched(inst.iid, r)
    mon.first_token(inst.iid, r.rid)
    mon.output_token(inst.iid, r.rid)  # the decode leg starts generating
    clock.advance(0.075)  # 1.5x target on one of two, so the pool averages 0.75
    mon.output_token(inst.iid, r.rid)
    mon.roll_interval()

    assert sched.pool_load(Role.PREFILL) == 0.0
    assert sched.pool_load(Role.DECODE) < sched.th.expand
    for _ in range(sched.th.sustained_intervals - 1):
        assert sched.monitoring_pass() is None
    assert sched.monitoring_pass() is not None


def test_the_migration_interval_is_not_the_decode_instances_load(tmp_path):
    """Arrow §4.3's `t2 = q2 + c + q3 + p2` spans the transfer and the queue into the
    instance, not time the instance spent generating. It stays in the request's
    TPOT, which `bench.score` reads off the journal, and stays out of the
    instance's load: charged there, an overloaded prefill stage reads as decode
    pressure and Algorithm 2 strips the prefill pool it should be growing."""
    clock, mon, sched = build(tmp_path=tmp_path)
    origin = mon.pool(Role.PREFILL)[0]
    target = mon.pool(Role.DECODE)[0]

    r = Request(rid="r7", input_len=100)
    mon.dispatched(origin.iid, r)
    clock.advance(0.1)
    mon.first_token(origin.iid, r.rid)  # o1, on the prefill instance
    r.phase = Phase.DECODE
    mon.dispatched(target.iid, r)
    clock.advance(0.5)  # q2 + c + q3 + p2
    mon.output_token(target.iid, r.rid)  # o2, on the decode instance
    clock.advance(0.2)
    mon.output_token(target.iid, r.rid)  # o3: the first same-instance gap

    mon.roll_interval()
    assert mon.mean_token_interval(target.iid) == pytest.approx(0.2), "o2->o3 counts"
    assert mon.mean_token_interval(origin.iid) == 0.0
    del sched


def test_a_completed_request_leaves_nothing_behind(tmp_path):
    """The token stamp is keyed by request, so retiring it clears it wherever
    prefill ran."""
    _, mon, _ = build(tmp_path=tmp_path)
    origin = mon.pool(Role.PREFILL)[0]
    target = mon.pool(Role.DECODE)[0]

    for k in range(100):
        r = Request(rid=f"r{k}", input_len=100)
        mon.dispatched(origin.iid, r)
        mon.first_token(origin.iid, r.rid)
        r.phase = Phase.DECODE
        mon.dispatched(target.iid, r)
        mon.output_token(target.iid, r.rid)
        mon.finished(target.iid, r.rid)

    assert mon._last_token == {}, "100 completed requests must leave no stamps"


def test_decode_batch_excludes_the_prefill_queue(tmp_path):
    """Arrow §3.1 puts the decode interval on the tokens in the batch, and Arrow §5.4 puts
    only a chunk of a prefill request there, never the whole queue."""
    _, mon, _ = build(tmp_path=tmp_path)
    inst = mon.pool(Role.DECODE)[0]
    mon.dispatched(inst.iid, Request(rid="d8", input_len=300, phase=Phase.DECODE))
    mon.dispatched(inst.iid, Request(rid="p8", input_len=9000))

    assert inst.decode_tokens() == 300
    assert inst.prefill_tokens() == 9000


def test_load_is_a_ratio_against_the_slo_not_a_queue_count(tmp_path):
    """Arrow §5.5 defines both loads as ratios to their target, which is what makes
    one threshold meaningful across instances of different capability."""
    clock, mon, sched = build(tmp_path=tmp_path)
    inst = mon.pool(Role.DECODE)[0]
    r = Request(rid="d6", input_len=10, phase=Phase.DECODE)
    mon.dispatched(inst.iid, r)
    mon.first_token(inst.iid, r.rid)
    mon.output_token(inst.iid, r.rid)  # the decode leg starts generating
    clock.advance(0.05)  # exactly the TPOT target
    mon.output_token(inst.iid, r.rid)
    mon.roll_interval()
    assert sched.decode_load(inst) == pytest.approx(1.0)


# -- Arrow §5.2 profiling -----------------------------------------------------


def test_prefill_is_fitted_quadratically_and_decode_linearly(tmp_path):
    """Arrow §3.1: prefill load "scales quadratically with the input length", decode
    "grows linearly with the total number of tokens in the batch"."""
    a, b, c = fit_quadratic([(0, 1.0), (10, 3.0), (20, 9.0), (30, 19.0)])
    assert a == pytest.approx(0.02, abs=1e-6)
    assert b == pytest.approx(0.0, abs=1e-6)
    assert c == pytest.approx(1.0, abs=1e-6)

    m, k = fit_linear([(0, 0.01), (1000, 0.02)])
    assert m == pytest.approx(1e-5)
    assert k == pytest.approx(0.01)


def test_an_instance_that_misses_tpot_at_every_batch_has_no_headroom(tmp_path):
    """§5.3's `MT(i, SLO_TPOT)` is "the maximum number of tokens instance i can
    compute concurrently under the given TPOT SLO". An instance whose interval
    misses the target at an empty batch can hold none of them."""
    p = Profile("i0", 1e-8, 1e-3, 0.0, tpot_slope=0.0, tpot_intercept=0.2)
    assert p.max_tokens(0.05) == 0.0

    # The slope only means "never degrades" once the floor already clears.
    ok = Profile("i0", 1e-8, 1e-3, 0.0, tpot_slope=0.0, tpot_intercept=0.01)
    assert ok.max_tokens(0.05) == float("inf")


def test_a_decode_request_needing_no_migration_skips_the_queue(tmp_path):
    """Arrow §5.4 places a request in the migration queue only "if" migration is
    required, so one that needs none never waits behind a transfer."""
    from narwhal.local import LocalScheduler

    ls = LocalScheduler()
    ls.admit_decode("migrating", ready_at=5.0)
    ls.admit_decode("local")

    assert ls.decode_ready == ["local"]
    assert ls.release_migrations(0.0) == []
    assert ls.decode_ready == ["local"], "and is not held by the transfer ahead of it"


def test_a_profile_survives_a_restart_and_reprofiles_alone(tmp_path):
    """Arrow §5.2: cached to disk, "only that specific instance needs to be
    re-profiled"."""
    path = tmp_path / "p.json"
    store = ProfileStore(path)
    store.put(Profile("i0", 1e-8, 1e-3, 0.0, 1e-5, 0.0))
    store.put(Profile("i1", 2e-8, 2e-3, 0.0, 2e-5, 0.0))

    reloaded = ProfileStore(path)
    assert len(reloaded) == 2
    reloaded.put(Profile("i1", 9e-8, 9e-3, 0.0, 9e-5, 0.0))
    assert ProfileStore(path).get("i0").ttft_b == pytest.approx(1e-3)


# -- Arrow §5.4 local scheduler ----------------------------------------------


def _profile() -> Profile:
    return Profile(
        "i0", ttft_a=2e-8, ttft_b=6e-5, ttft_c=0.005, tpot_slope=3e-6, tpot_intercept=0.012
    )


def test_decode_is_admitted_to_the_batch_before_prefill(tmp_path):
    """Arrow §5.4: "decode requests are prioritized to be included in the running
    batch. If there is remaining space, chunked prefill requests are added"."""
    from narwhal.local import LocalScheduler

    ls = LocalScheduler(batch_tokens=4096, chunk_max=2048)
    ls.admit_prefill("p1", 9000)
    ls.admit_decode("d1", ready_at=0.0)
    ls.release_migrations(0.0)

    it = ls.step(_profile(), {"d1": 4096})
    assert it.decoded == ["d1"], "decode must be in the batch"
    assert it.prefilled == [], "a full decode batch leaves no room for a chunk"


def test_the_batch_size_bounds_decode_membership(tmp_path):
    """Arrow §5.4: "Under a given batch size, decode requests are prioritized to be
    included in the running batch". The size bounds the batch, so a request
    that does not fit waits rather than joining it."""
    from narwhal.local import LocalScheduler

    ls = LocalScheduler(batch_tokens=4096, chunk_max=2048)
    for k in range(8):
        ls.admit_decode(f"d{k}", ready_at=0.0)
    ls.release_migrations(0.0)

    it = ls.step(_profile(), {f"d{k}": 1000 for k in range(8)})
    assert it.decoded == ["d0", "d1", "d2", "d3"], "4000 fits, the fifth would not"
    assert ls.decode_ready == [f"d{k}" for k in range(8)], "the rest keep their place"


def test_an_oversized_decode_request_still_runs(tmp_path):
    """A request larger than the whole batch would otherwise never be admitted."""
    from narwhal.local import LocalScheduler

    ls = LocalScheduler(batch_tokens=4096, chunk_max=2048)
    ls.admit_decode("huge", ready_at=0.0)
    ls.release_migrations(0.0)
    assert ls.step(_profile(), {"huge": 99999}).decoded == ["huge"]


def test_chunks_fill_the_space_decode_left(tmp_path):
    """Arrow §5.4: "If there is remaining space, chunked prefill requests are added",
    plural, so one queued prompt does not hold the whole budget."""
    from narwhal.local import LocalScheduler

    ls = LocalScheduler(batch_tokens=8192, chunk_max=2048)
    for k in range(6):
        ls.admit_prefill(f"p{k}", 4000)

    it = ls.step(_profile(), {})
    assert it.prefilled == ["p0", "p1", "p2", "p3"], "8192 of budget takes four chunks"
    assert it.completed_prefill == []


def test_a_long_prompt_never_blocks_a_freshly_flipped_instance(tmp_path):
    """Arrow §5.4's stated purpose: chunking avoids "the situation where requests
    queued before instance flipping block the execution of new requests after
    flipping"."""
    from narwhal.local import LocalScheduler

    ls = LocalScheduler(batch_tokens=8192, chunk_max=2048)
    ls.admit_prefill("long", 20000)  # queued before the flip

    # The instance is relabelled and a decode request arrives.
    ls.admit_decode("new", ready_at=0.0)
    ls.release_migrations(0.0)

    it = ls.step(_profile(), {"new": 1000})
    assert "new" in it.decoded, "the new request runs on the very next iteration"
    assert it.prefilled == ["long"], "and the long prompt still progresses, in a chunk"
    assert "long" not in it.completed_prefill


def test_chunking_does_not_change_total_prefill_cost(tmp_path):
    """A chunk is priced as the exact difference of the profiled curve, so the
    sum over chunks equals the unchunked total."""
    from narwhal.local import LocalScheduler

    profile = _profile()
    ls = LocalScheduler(batch_tokens=99999, chunk_max=1000)
    ls.admit_prefill("p", 5000)
    total = 0.0
    for _ in range(5):
        it = ls.step(profile, {})
        total += it.seconds
    assert total == pytest.approx(profile.prefill_time(5000) - profile.prefill_time(0))
    assert ls.prefill_queue == deque()


def test_kv_migration_is_fcfs(tmp_path):
    """Arrow §5.4: "The local scheduler adopts a FCFS policy for KV Cache migration"."""
    from narwhal.local import LocalScheduler

    ls = LocalScheduler()
    ls.admit_decode("first", ready_at=1.0)
    ls.admit_decode("second", ready_at=0.1)

    assert ls.release_migrations(0.5) == [], "a later arrival must not overtake"
    assert ls.release_migrations(1.0) == ["first", "second"]


def test_prefill_work_on_a_decode_labelled_instance_still_counts(tmp_path):
    """Algorithm 1 costs every instance, so prefill lands wherever it is
    cheapest. Read off the prefill pool alone, the load misses that work and
    reports an idle pool while TTFT is missing."""
    _, mon, sched = build(n_prefill=1, n_decode=3, tmp_path=tmp_path)
    target = mon.pool(Role.DECODE)[0]
    for k in range(20):
        mon.dispatched(target.iid, Request(rid=f"p{k}", input_len=2000))

    assert sched.prefill_load(target) > 1.0, "the instance is over its TTFT budget"
    assert sched.pool_load(Role.PREFILL) > 0.0, "and the fleet's prefill load says so"


def test_an_idle_fleet_still_reads_zero_prefill_load(tmp_path):
    _, _, sched = build(tmp_path=tmp_path)
    assert sched.pool_load(Role.PREFILL) == 0.0


def test_the_shrink_trigger_stops_firing_while_prefill_is_busy(tmp_path):
    """Algorithm 2's `LP <= LSHRINK <= LD` must not lend prefill away while
    instances are carrying prefill work it could fail to see."""
    clock, mon, sched = build(n_prefill=2, n_decode=2, tmp_path=tmp_path)
    for inst in mon.pool(Role.DECODE):
        r = Request(rid=f"d{inst.iid}", input_len=10, phase=Phase.DECODE)
        mon.dispatched(inst.iid, r)
        mon.first_token(inst.iid, r.rid)
        clock.advance(0.04)
        mon.output_token(inst.iid, r.rid)
    # Prefill work parked on a decode-labelled instance, as Algorithm 1 does.
    busy = mon.pool(Role.DECODE)[0]
    for k in range(20):
        mon.dispatched(busy.iid, Request(rid=f"q{k}", input_len=2000))

    assert sched.pool_load(Role.PREFILL) > sched.th.shrink
    assert sched.monitoring_pass() is None, "no P->D flip while prefill is loaded"


def test_a_prefill_request_is_priced_over_the_prefill_pool(tmp_path):
    """Arrow §5.5's D->P trigger reads "the current prefill instances cannot meet the
    TTFT SLO". Costing decode instances too makes step 3 unreachable, because
    an instance batching decode always looks cheap against a profile measured
    with no decode load."""
    _, mon, sched = build(n_prefill=1, n_decode=3, tmp_path=tmp_path)
    only_prefill = mon.pool(Role.PREFILL)[0]
    for k in range(20):
        mon.dispatched(only_prefill.iid, Request(rid=f"p{k}", input_len=2000))

    before = len(mon.pool(Role.PREFILL))
    chosen = sched.schedule(Request(rid="new", input_len=2000))

    assert sched.unserved == 1, "step 3 has to be reached when prefill cannot serve"
    assert len(mon.pool(Role.PREFILL)) == before + 1, "and a decode instance joins prefill"
    assert chosen.role is Role.PREFILL


def test_an_empty_pool_falls_back_to_the_whole_fleet(tmp_path):
    """The aggregated arm labels every engine decode, so a prefill request has
    no pool of its own and still has to be served."""
    _, mon, sched = build(n_prefill=0, n_decode=4, tmp_path=tmp_path)
    chosen = sched.schedule(Request(rid="r", input_len=100))
    assert chosen.iid in mon.instances


def test_scheduling_an_unprofiled_instance_names_the_fix(tmp_path):
    """Algorithm 1 prices every candidate from its profile, so a missing one is
    a refusal rather than a default curve."""
    _, mon, sched = build(tmp_path=tmp_path)
    mon.add(Instance(iid="new", url="http://new", role=Role.PREFILL))
    with pytest.raises(KeyError, match="profile before scheduling"):
        sched.schedule(Request(rid="r", input_len=100))


def test_excluding_every_instance_refuses_rather_than_picking_one(tmp_path):
    """The retry path excludes what already failed. Excluding the whole fleet
    has to raise, not fall through to the instance that just timed out."""
    _, mon, sched = build(tmp_path=tmp_path)
    everything = set(mon.instances)
    with pytest.raises(RuntimeError, match="no schedulable instances"):
        sched.schedule(Request(rid="r", input_len=100), exclude=everything)


def test_a_flip_records_which_algorithm_asked(tmp_path):
    """Algorithm 1 flips inline on the request path and Algorithm 2 on the
    monitoring loop. A pool that reverses is only diagnosable if the record
    says which one moved it."""
    clock, mon, sched = build(n_prefill=3, n_decode=3, tmp_path=tmp_path)

    # Algorithm 2: decode misses TPOT, so the loop grows the decode pool.
    for inst in mon.pool(Role.DECODE):
        r = Request(rid=f"d{inst.iid}", input_len=10, phase=Phase.DECODE)
        mon.dispatched(inst.iid, r)
        mon.first_token(inst.iid, r.rid)
        clock.advance(0.2)
        mon.output_token(inst.iid, r.rid)
    for _ in range(sched.th.sustained_intervals - 1):
        assert sched.monitoring_pass() is None
    assert sched.monitoring_pass() is not None
    assert sched.flips[-1].by == "algorithm2"

    # Algorithm 1: no prefill instance can meet the TTFT target for a new one,
    # and decode is genuinely idle rather than stalled, so the D->P is allowed.
    clock.advance(sched.th.cooldown_s)
    for inst in mon.pool(Role.DECODE):
        mon.finished(inst.iid, f"d{inst.iid}")
    mon.roll_interval()
    mon.roll_interval()
    for inst in mon.pool(Role.PREFILL):
        for k in range(3):
            mon.dispatched(inst.iid, Request(rid=f"p{inst.iid}{k}", input_len=2000))
    sched.monitor.roll_interval()
    assert sched.pool_load(Role.DECODE) < sched.th.shrink
    sched.schedule(Request(rid="hot", input_len=2000))
    assert sched.flips[-1].by == "algorithm1"
    assert sched.flips[-1].to is Role.PREFILL


def test_the_two_algorithms_can_reverse_the_same_instance(tmp_path):
    """Arrow §5.5 adds the cooldown "to prevent oscillation in instance assignment",
    but applies it to P->D alone, so a D->P can follow immediately."""
    _, _, sched = build(n_prefill=3, n_decode=3, tmp_path=tmp_path)

    moved = sched.flip(Role.DECODE, "algorithm2")
    assert moved is not None
    back = sched.flip(Role.PREFILL, "algorithm1")

    assert back is not None, "D->P is ungated, so it can fire in the same instant"
    assert [f.by for f in sched.flips] == ["algorithm2", "algorithm1"]


def test_the_two_flip_windows_do_not_overlap(tmp_path):
    """Algorithm 2's shrink branch needs `LD >= shrink`; Algorithm 1's D->P
    needs decode load low. Reading "low" as `expand` leaves the band between
    the thresholds where both are legal, and the pool reverses there."""
    _, _, sched = build(tmp_path=tmp_path)
    th = sched.th
    between = (th.shrink + th.expand) / 2

    algorithm2_may_fire = between >= th.shrink
    algorithm1_may_fire = between < th.shrink
    assert algorithm2_may_fire
    assert not algorithm1_may_fire, "both legal at the same decode load is the flap"


def test_a_loaded_decode_pool_still_refuses_at_the_lower_threshold(tmp_path):
    """Arrow §5.5's overload rule: the D->P flip aborts while decode is carrying
    work, which now means at or above `shrink` rather than `expand`."""
    clock, mon, sched = build(n_prefill=1, n_decode=3, tmp_path=tmp_path)
    for inst in mon.instances.values():
        mon.dispatched(inst.iid, Request(rid=f"p{inst.iid}", input_len=2000))
    # Drive decode load between shrink and expand, where it used to flip.
    for inst in mon.pool(Role.DECODE):
        r = Request(rid=f"d{inst.iid}", input_len=10, phase=Phase.DECODE)
        mon.dispatched(inst.iid, r)
        mon.first_token(inst.iid, r.rid)
        clock.advance(0.0375)  # 0.75 of a 50 ms target
        mon.output_token(inst.iid, r.rid)

    ld = sched.pool_load(Role.DECODE)
    assert sched.th.shrink <= ld < sched.th.expand, "the band this test is about"
    before = len(mon.pool(Role.DECODE))
    sched.schedule(Request(rid="hot", input_len=2000))
    assert len(mon.pool(Role.DECODE)) == before, "decode keeps its instances in the band"
    assert sched.flips_refused, "and the refusal is recorded with the reason"


def test_a_stalled_instance_does_not_read_as_idle(tmp_path):
    """An empty average reads 0, so an instance that stalls harder reads more
    idle unless the open gap backstops it."""
    clock, mon, sched = build(tmp_path=tmp_path)
    inst = mon.pool(Role.DECODE)[0]
    r = Request(rid="d1", input_len=10, phase=Phase.DECODE)
    mon.dispatched(inst.iid, r)
    mon.first_token(inst.iid, r.rid)
    mon.output_token(inst.iid, r.rid)  # generation started, then went quiet
    sched.monitor.roll_interval()  # close the window with no gaps in it

    clock.advance(1.0)  # 20x a 50 ms target, still nothing
    assert mon.mean_token_interval(inst.iid) == 0.0, "the average really is empty"
    assert sched.decode_load(inst) == pytest.approx(1.0 / 0.05)


def test_a_request_waiting_for_its_first_decode_token_is_not_a_stall(tmp_path):
    """The wait before the first decode token is Arrow §4.3's `q2 + c + q3`, the
    transfer and the queue into the instance, bounded by the engine client's
    first-token deadline. Counted as an open inter-token gap, prefill backlog
    reads as decode pressure: the longer prompts queue upstream, the harder the
    expand trigger fires."""
    clock, mon, sched = build(tmp_path=tmp_path)
    inst = mon.pool(Role.DECODE)[0]
    r = Request(rid="d1", input_len=10, phase=Phase.DECODE)
    mon.dispatched(inst.iid, r)
    mon.first_token(inst.iid, r.rid)  # o1 exists; the decode leg has not started
    mon.roll_interval()

    clock.advance(1.0)  # a long transfer, not a stall of this instance
    assert mon.stalled_gap(inst.iid) == 0.0
    assert sched.decode_load(inst) == 0.0


def test_prefill_load_survives_a_pass_between_arrivals(tmp_path):
    """Prefill work is resident from dispatch to o1, so at low rates the set
    is empty at most instants and a snapshot arms the shrink trigger by
    default. The interval average holds what was resident over the completed
    window, so a pass between arrivals still sees the interval's work."""
    clock, mon, sched = build(tmp_path=tmp_path)
    inst = mon.pool(Role.PREFILL)[0]
    r = Request(rid="p1", input_len=1000)
    mon.dispatched(inst.iid, r)
    clock.advance(1.0)  # resident for one second...
    mon.first_token(inst.iid, r.rid)
    clock.advance(1.0)  # ...then gone for one second
    mon.roll_interval()

    assert not inst.prefill, "nothing is resident at pass time"
    # price(1000) = 1.01 s, resident for half the two-second window.
    assert sched.prefill_load(inst) == pytest.approx(1.01 / 2.0)


def test_decode_load_reads_idle_at_the_profiled_cadence(tmp_path):
    """With a TPOT target at twice the cadence, the raw ratio on an unloaded
    pool sits above shrink, arming the shrink trigger on every pass and
    refusing every D->P recovery. Above the profiled floor, the natural
    cadence reads idle and the SLO reads 1.0."""
    clock, mon, sched = build(tmp_path=tmp_path)
    inst = mon.pool(Role.DECODE)[0]
    # A tight regime: a 32 ms floor under the fixture's 50 ms target.
    sched.profiles.put(Profile(inst.iid, 1e-8, 1e-3, 0.0, tpot_slope=1.25e-5, tpot_intercept=0.032))
    r = Request(rid="d1", input_len=10, phase=Phase.DECODE)
    mon.dispatched(inst.iid, r)
    mon.first_token(inst.iid, r.rid)
    mon.output_token(inst.iid, r.rid)
    clock.advance(0.032)  # the engine's natural cadence
    mon.output_token(inst.iid, r.rid)
    mon.roll_interval()

    assert sched.th.shrink < 0.032 / 0.05, "the raw ratio would arm the trigger"
    assert sched.decode_load(inst) == pytest.approx(0.0)

    clock.advance(0.05)  # a gap at the SLO
    mon.output_token(inst.iid, r.rid)
    mon.roll_interval()
    assert sched.decode_load(inst) == pytest.approx(1.0)


def test_a_floor_past_the_slo_falls_back_to_the_raw_ratio(tmp_path):
    """§5.3's `MT` reads 0 headroom when the intercept misses the target at an
    empty batch, so there is no idle band to normalize away."""
    clock, mon, sched = build(tmp_path=tmp_path)
    inst = mon.pool(Role.DECODE)[0]
    sched.profiles.put(Profile(inst.iid, 1e-8, 1e-3, 0.0, tpot_slope=1.25e-5, tpot_intercept=0.2))
    r = Request(rid="d1", input_len=10, phase=Phase.DECODE)
    mon.dispatched(inst.iid, r)
    mon.first_token(inst.iid, r.rid)
    mon.output_token(inst.iid, r.rid)
    clock.advance(0.2)
    mon.output_token(inst.iid, r.rid)
    mon.roll_interval()
    assert sched.decode_load(inst) == pytest.approx(0.2 / 0.05)


def test_recovery_is_not_refused_at_the_natural_cadence(tmp_path):
    """A raw decode ratio at the natural cadence refuses every D->P recovery,
    so the pool can only ever drift toward decode. A pool that is idle above
    its floor must give an instance back."""
    clock, mon, sched = build(n_prefill=1, n_decode=3, tmp_path=tmp_path)
    decode_pool = mon.pool(Role.DECODE)
    for inst in decode_pool:
        sched.profiles.put(
            Profile(inst.iid, 1e-8, 1e-3, 0.0, tpot_slope=1.25e-5, tpot_intercept=0.032)
        )
        r = Request(rid=f"d{inst.iid}", input_len=10, phase=Phase.DECODE)
        mon.dispatched(inst.iid, r)
        mon.first_token(inst.iid, r.rid)
        mon.output_token(inst.iid, r.rid)
    clock.advance(0.032)  # one natural-cadence gap on every instance
    for inst in decode_pool:
        mon.output_token(inst.iid, f"d{inst.iid}")
    mon.roll_interval()

    # The only prefill instance is past the TTFT budget for the next request.
    only = mon.pool(Role.PREFILL)[0]
    for k in range(3):
        mon.dispatched(only.iid, Request(rid=f"p{k}", input_len=2000))

    before = len(mon.pool(Role.PREFILL))
    sched.schedule(Request(rid="hot", input_len=2000))
    assert len(mon.pool(Role.PREFILL)) == before + 1, "the D->P recovery goes through"


def test_an_instance_with_no_decode_work_still_reads_idle(tmp_path):
    clock, mon, sched = build(tmp_path=tmp_path)
    clock.advance(30.0)
    assert sched.decode_load(mon.pool(Role.DECODE)[0]) == 0.0
    assert sched.pool_load(Role.DECODE) == 0.0


def test_a_stalled_pool_refuses_to_give_up_an_instance(tmp_path):
    """The guard that stops the D->P runaway only works if the stall is seen."""
    clock, mon, sched = build(n_prefill=1, n_decode=3, tmp_path=tmp_path)
    for inst in mon.instances.values():
        mon.dispatched(inst.iid, Request(rid=f"p{inst.iid}", input_len=2000))
    for inst in mon.pool(Role.DECODE):
        r = Request(rid=f"d{inst.iid}", input_len=10, phase=Phase.DECODE)
        mon.dispatched(inst.iid, r)
        mon.first_token(inst.iid, r.rid)
        mon.output_token(inst.iid, r.rid)  # generation started, then went quiet
    clock.advance(2.0)

    assert sched.pool_load(Role.DECODE) >= sched.th.shrink
    before = len(mon.pool(Role.DECODE))
    sched.schedule(Request(rid="hot", input_len=2000))
    assert len(mon.pool(Role.DECODE)) == before


def test_decode_load_holds_still_across_an_interval(tmp_path):
    """Arrow §5.5 reads the load off a completed interval. Recomputed mid-interval it
    is a sawtooth, and Algorithm 1 on the request path and Algorithm 2 on the
    loop read different numbers and reverse each other."""
    clock, mon, sched = build(tmp_path=tmp_path)
    inst = mon.pool(Role.DECODE)[0]
    r = Request(rid="d1", input_len=10, phase=Phase.DECODE)
    mon.dispatched(inst.iid, r)
    mon.first_token(inst.iid, r.rid)
    mon.output_token(inst.iid, r.rid)  # the decode leg starts generating
    clock.advance(0.1)
    mon.output_token(inst.iid, r.rid)
    mon.roll_interval()  # close the interval, publish 0.1

    published = sched.decode_load(inst)
    assert published == pytest.approx(0.1 / 0.05)

    # More tokens arrive; the published value must not move until the next roll.
    clock.advance(0.01)
    mon.output_token(inst.iid, r.rid)
    assert sched.decode_load(inst) == pytest.approx(published)

    mon.roll_interval()
    assert sched.decode_load(inst) == pytest.approx(0.01 / 0.05)


def test_an_interval_with_no_tokens_keeps_the_last_published_value(tmp_path):
    """A quiet interval is not evidence the instance got faster."""
    clock, mon, _ = build(tmp_path=tmp_path)
    inst = mon.pool(Role.DECODE)[0]
    r = Request(rid="d1", input_len=10, phase=Phase.DECODE)
    mon.dispatched(inst.iid, r)
    mon.first_token(inst.iid, r.rid)
    mon.output_token(inst.iid, r.rid)  # the decode leg starts generating
    clock.advance(0.1)
    mon.output_token(inst.iid, r.rid)
    mon.roll_interval()
    mon.roll_interval()  # a second, empty interval
    assert mon.mean_token_interval(inst.iid) == pytest.approx(0.1)


def test_a_flip_records_what_it_was_carrying(tmp_path):
    """§C asks for the in-flight count at each migration: a flip that strands
    work costs more than the relabel it looks like."""
    _, mon, sched = build(n_prefill=3, n_decode=3, tmp_path=tmp_path)
    busy = mon.pool(Role.PREFILL)[0]
    for k in range(4):
        mon.dispatched(busy.iid, Request(rid=f"p{k}", input_len=100))
    mon.dispatched(busy.iid, Request(rid="d0", input_len=10, phase=Phase.DECODE))

    moved = sched.flip(Role.DECODE, "algorithm2")

    assert moved.iid == busy.iid, "the incomplete flip sorts first (Arrow §5.5)"
    assert sched.flips[-1].prefill_inflight == 4
    assert sched.flips[-1].decode_inflight == 1


def test_an_idle_flip_carries_nothing(tmp_path):
    _, _, sched = build(n_prefill=3, n_decode=3, tmp_path=tmp_path)
    sched.flip(Role.DECODE, "algorithm2")
    assert sched.flips[-1].prefill_inflight == 0
    assert sched.flips[-1].decode_inflight == 0


def test_a_flip_records_how_long_its_caught_work_took_to_clear(tmp_path):
    """§E: the role change is a relabel, so what a flip costs is the work it
    caught still finishing under the old role."""
    clock, mon, sched = build(n_prefill=3, n_decode=3, tmp_path=tmp_path)
    busy = mon.pool(Role.PREFILL)[0]
    # Decode work resident makes its indicator sort first (Arrow §5.5), so this is the
    # instance Algorithm 3 picks; the prefill work is what the flip then catches.
    mon.dispatched(busy.iid, Request(rid="d0", input_len=10, phase=Phase.DECODE))
    mon.dispatched(busy.iid, Request(rid="p0", input_len=100))

    moved = sched.flip(Role.DECODE, "algorithm2")
    assert moved.iid == busy.iid
    assert sched.flips[-1].drained_s is None, "the work is still there"

    clock.advance(3.0)
    sched.settle_drains()
    assert sched.flips[-1].drained_s is None, "still there after three seconds"

    mon.first_token(busy.iid, "p0")
    clock.advance(1.0)
    sched.settle_drains()
    assert sched.flips[-1].drained_s == pytest.approx(4.0)


def test_a_flip_that_caught_nothing_drains_at_once(tmp_path):
    clock, _, sched = build(n_prefill=3, n_decode=3, tmp_path=tmp_path)
    sched.flip(Role.DECODE, "algorithm2")
    clock.advance(0.5)
    sched.settle_drains()
    assert sched.flips[-1].drained_s == pytest.approx(0.5)


def test_a_single_spike_does_not_move_the_pool(tmp_path):
    """Arrow §5.5 fires Algorithm 2 when the load "exceeds a threshold over a period
    of time". One interval over the line is a spike, not a period."""
    clock, mon, sched = build(n_prefill=3, n_decode=3, tmp_path=tmp_path)
    inst = mon.pool(Role.DECODE)[0]
    r = Request(rid="d1", input_len=10, phase=Phase.DECODE)
    mon.dispatched(inst.iid, r)
    mon.first_token(inst.iid, r.rid)
    clock.advance(0.2)
    mon.output_token(inst.iid, r.rid)

    assert sched.monitoring_pass() is None, "one crossing moves nothing"
    mon.finished(inst.iid, r.rid)
    assert sched.monitoring_pass() is None
    assert sched.flips == [], "and the spike leaves no flip behind"


def test_the_count_resets_when_the_load_drops_back(tmp_path):
    """Two crossings either side of a quiet interval are not a period."""
    clock, mon, sched = build(n_prefill=3, n_decode=3, tmp_path=tmp_path)
    inst = mon.pool(Role.DECODE)[0]
    r = Request(rid="d1", input_len=10, phase=Phase.DECODE)
    mon.dispatched(inst.iid, r)
    mon.first_token(inst.iid, r.rid)

    def over_threshold():
        clock.advance(0.2)
        mon.output_token(inst.iid, r.rid)
        return sched.monitoring_pass()

    assert over_threshold() is None
    mon.finished(inst.iid, r.rid)  # load falls away
    assert sched.monitoring_pass() is None
    mon.dispatched(inst.iid, r)
    mon.first_token(inst.iid, r.rid)
    assert over_threshold() is None, "the count restarted, so this is only the first again"


def test_a_pinned_cooldown_gates_the_first_flip_too(tmp_path):
    """A harness pins an arm with a cooldown no run outlives. Seeded with
    -inf, the cooldown never gated the first P->D, and the pinned arm moved
    once mid-run."""
    clock, mon, sched = build(n_prefill=3, n_decode=1, tmp_path=tmp_path)
    sched.th.cooldown_s = 1e9

    assert sched.flip(Role.DECODE, "algorithm1") is None, "pinned means the first flip too"
    assert len(mon.pool(Role.PREFILL)) == 3

    clock.advance(1e9)
    assert sched.flip(Role.DECODE, "algorithm1") is not None, "a spent cooldown releases"


def test_a_dwelling_instance_sits_out_both_directions(tmp_path):
    """§F's dwell: the instance that just flipped is unavailable, in either
    direction, until the dwell passes - which is what stops two algorithms
    trading the same engine at a phase boundary."""
    clock, mon, sched = build(n_prefill=3, n_decode=2, tmp_path=tmp_path)
    sched.th.dwell_s = 30.0

    moved = sched.flip(Role.DECODE, "algorithm2")
    assert moved is not None

    # The pool holds a rested prefill instance, so P->D can still fire...
    clock.advance(sched.th.cooldown_s)
    second = sched.flip(Role.DECODE, "algorithm2")
    assert second is not None
    assert second.iid != moved.iid

    # ...but D->P finds every decode candidate either dwelling or original.
    rested_decode = [i for i in mon.pool(Role.DECODE) if i.iid not in (moved.iid, second.iid)]
    for inst in rested_decode:
        got = sched.flip(Role.PREFILL, "algorithm1")
        assert got is not None, "rested instances go first"
        assert got.iid == inst.iid, "rested instances go first"
    assert sched.flip(Role.PREFILL, "algorithm1") is None, "only dwellers remain"

    clock.advance(30.0)
    assert sched.flip(Role.PREFILL, "algorithm1") is not None, "the dwell passed"


def test_zero_dwell_is_the_papers_behaviour(tmp_path):
    """The default leaves Arrow §5.5 exactly as written: D->P ungated, immediately."""
    _clock, _, sched = build(n_prefill=3, n_decode=3, tmp_path=tmp_path)
    assert sched.th.dwell_s == 0.0
    moved = sched.flip(Role.DECODE, "algorithm2")
    back = sched.flip(Role.PREFILL, "algorithm1")
    assert moved is not None
    assert back is not None
    assert moved.iid == back.iid, "without a dwell the same instance trades hands"


def _loads(sched, lp, ld):
    from narwhal.types import Role as R

    sched.pool_load = lambda role: lp if role is R.PREFILL else ld


def test_the_panic_bypass_is_off_by_default(tmp_path):
    """The Arrow paper has no bypass, so panic_ratio 0 must reproduce the
    paper's cooldown exactly, whatever the loads say."""
    _, _, sched = build(n_prefill=3, n_decode=3, tmp_path=tmp_path)
    _loads(sched, 0.0, 99.0)
    for _ in range(5):
        sched._note_panic(0.0, 99.0)
    assert sched.flip(Role.DECODE) is not None
    assert sched.flip(Role.DECODE) is None, "no bypass at panic_ratio 0"
    assert sched.panic_bypasses == 0


def test_the_cooldown_yields_to_a_sustained_regime_flip(tmp_path):
    """Decode past the panic multiple while prefill idles, held for the
    sustained count: the one shape of overload the cooldown must not
    damp. Below the sustained count the cooldown holds."""
    _, _, sched = build(n_prefill=4, n_decode=2, tmp_path=tmp_path)
    sched.th.panic_ratio = 2.0
    _loads(sched, 0.1, 2.5)
    assert sched.flip(Role.DECODE) is not None  # spends the cooldown

    sched._note_panic(0.1, 2.5)
    sched._note_panic(0.1, 2.5)
    assert sched.flip(Role.DECODE) is None, "two passes are not sustained"
    assert sched.panic_bypasses == 0

    sched._note_panic(0.1, 2.5)
    assert sched.flip(Role.DECODE) is not None, "three sustained passes yield"
    assert sched.panic_bypasses == 1


def test_a_global_spike_never_fires_the_bypass(tmp_path):
    """The hardware lesson from the bypass splice campaign: a global
    spike raises both loads, and the one-sided bypass stripped the
    prefill pool mid-flood, halving goodput. Both loads high must reset
    the signal and refuse the bypass, indefinitely."""
    _, _, sched = build(n_prefill=4, n_decode=2, tmp_path=tmp_path)
    sched.th.panic_ratio = 2.0
    _loads(sched, 3.0, 3.0)
    assert sched.flip(Role.DECODE) is not None  # spends the cooldown
    for _ in range(10):
        sched._note_panic(3.0, 3.0)
    assert sched.flip(Role.DECODE) is None, "both pools loaded is a flood, not a flip"
    assert sched.panic_bypasses == 0
    assert sched._panic_sustained == 0, "a global spike resets the signal"


def test_the_panic_signal_resets_when_the_regime_passes(tmp_path):
    _, _, sched = build(n_prefill=4, n_decode=2, tmp_path=tmp_path)
    sched.th.panic_ratio = 2.0
    for _ in range(3):
        sched._note_panic(0.1, 2.5)
    assert sched._panic_sustained == 3
    sched._note_panic(0.6, 2.5)  # prefill back above shrink
    assert sched._panic_sustained == 0


def test_affinity_off_is_pure_argmin(tmp_path):
    """The shipped position: no cache ownership, prefix identity ignored."""
    _, mon, sched = build(n_prefill=2, n_decode=2, tmp_path=tmp_path)
    a = sched.schedule(Request(rid="r1", input_len=100, prefix_key=42))
    mon.dispatched(a.iid, Request(rid="r1", input_len=20000))
    b = sched.schedule(Request(rid="r2", input_len=100, prefix_key=42))
    assert b.iid != a.iid, "with affinity off, load moves the placement"


def test_affinity_on_returns_to_the_warm_engine(tmp_path):
    """The selfish play: the warm engine wins outright, costs be
    damned - the private benefit the caching game is made of."""
    _, mon, sched = build(n_prefill=2, n_decode=2, tmp_path=tmp_path)
    sched.prefill_affinity = True
    r1 = Request(rid="r1", input_len=100, prefix_key=42)
    a = sched.schedule(r1)
    # Teaching happens at dispatch (a refused request must not warm the map).
    sched.remember(r1, a)
    mon.dispatched(a.iid, Request(rid="r1", input_len=20000))
    b = sched.schedule(Request(rid="r2", input_len=100, prefix_key=42))
    assert b.iid == a.iid, "the warm engine wins despite its load"
    c = sched.schedule(Request(rid="r3", input_len=100, prefix_key=99))
    assert c.iid != a.iid, "a different prefix is placed by cost"


def test_the_affinity_map_is_capped(tmp_path):
    _, _, sched = build(n_prefill=2, n_decode=2, tmp_path=tmp_path)
    sched.prefill_affinity = True
    for k in range(300):
        sched.schedule(Request(rid=f"r{k}", input_len=100, prefix_key=k))
    assert len(sched._affinity) <= 256


def test_a_batch_splits_where_greedy_would_collide(tmp_path):
    """Batched placement: two same-size requests arriving together both argmin to the
    same idle engine under greedy; the joint assignment spreads them."""
    _, _, sched = build(n_prefill=2, n_decode=2, tmp_path=tmp_path)
    a = Request(rid="a", input_len=500)
    b = Request(rid="b", input_len=500)
    # Greedy places sequentially and the monitor is only told on dispatch,
    # so both picks land on the same engine; the batch must not.
    assert sched.schedule(a).iid == sched.schedule(b).iid
    placed = sched.schedule_batch([a, b])
    assert placed["a"].iid != placed["b"].iid, "joint assignment spreads the window"
    assert {placed["a"].role, placed["b"].role} == {Role.PREFILL}


def test_a_lone_request_batches_like_greedy(tmp_path):
    _, _, sched = build(n_prefill=2, n_decode=2, tmp_path=tmp_path)
    r = Request(rid="solo", input_len=500)
    assert sched.schedule_batch([r])["solo"].iid == sched.schedule(r).iid


def test_an_overfull_window_degrades_to_greedy_not_refusal(tmp_path):
    _, _, sched = build(n_prefill=2, n_decode=2, tmp_path=tmp_path)
    reqs = [Request(rid=f"r{k}", input_len=500) for k in range(5)]
    placed = sched.schedule_batch(reqs)
    assert len(placed) == 5, "every request in the window is placed"


def test_the_regret_gauge_prices_each_placement_against_its_own_floor(tmp_path):
    """Observation only: a placement on its cheapest candidate
    regrets 1.0; a forced dearer pick prices above it. Live regret is
    per-decision - the windowed matching optimum belongs to the offline
    replay, where one consistent state exists."""
    _, _, sched = build(n_prefill=3, n_decode=3, tmp_path=tmp_path)
    for _ in range(3):
        sched._regrets.append(1.0)
    assert sched.placement_regret() == 1.0
    for _ in range(6):
        sched._regrets.append(3.0)
    assert sched.placement_regret() == 3.0


def test_the_regime_classifier_follows_the_loads(tmp_path):
    _, _, sched = build(tmp_path=tmp_path)
    _loads(sched, 0.2, 0.3)
    assert sched.regime() == "subcritical"
    _loads(sched, 0.2, 1.2)
    assert sched.regime() == "transitional"
    _loads(sched, 0.2, 2.5)
    assert sched.regime() == "saturated"


def test_the_gauge_records_at_placement_time_under_any_controller(tmp_path):
    """The merged-main smoke's lesson: the gauge must not depend on
    Algorithm 2's pass. Regret records inside schedule() itself, so the
    planner default measures identically."""
    _, _, sched = build(n_prefill=3, n_decode=3, tmp_path=tmp_path)
    sched.controller_owns_flips = True
    for k in range(4):
        sched.schedule(Request(rid=f"g{k}", input_len=400))
    assert sched.placement_regret() is not None
    assert len(sched._regrets) == 4


# -- re-placement of queued prefill legs --------------------------------


def _queue(mon, iid, rid, input_len, replaced=0):
    req = Request(rid=rid, input_len=input_len, replaced=replaced)
    mon.dispatched(iid, req)
    return req


def test_a_priced_out_queue_drains_toward_the_peer_capped_per_pass(tmp_path):
    """Everything in a queue priced past the budget is a move candidate; the
    cap ships the deepest misses first rather than inverting the skew."""
    _, mon, sched = build(tmp_path=tmp_path)
    for k in range(3):
        _queue(mon, "i0", f"s{k}", 500)
    _queue(mon, "i0", "leg", 500)
    moves = sched.queue_replacements(slack_s=0.05, limit=2)
    assert len(moves) == 2, "the caller's cap trims the nomination, deepest first"
    assert all(src == "i0" and dst == "i1" for _, src, dst in moves)


def test_a_leg_that_fits_nowhere_stays_put(tmp_path):
    _, mon, sched = build(tmp_path=tmp_path)
    for iid in ("i0", "i1"):
        for k in range(3):
            _queue(mon, iid, f"{iid}-{k}", 500)
    _queue(mon, "i0", "leg", 500)
    assert sched.queue_replacements(slack_s=0.05) == []


def test_a_leg_that_fits_where_it_sits_never_moves(tmp_path):
    _, mon, sched = build(tmp_path=tmp_path)
    _queue(mon, "i0", "leg", 500)
    assert sched.queue_replacements(slack_s=0.05) == []


def test_a_once_moved_leg_is_never_moved_again(tmp_path):
    """A leg that misses everywhere after one move must not ping-pong."""
    _, mon, sched = build(tmp_path=tmp_path)
    for k in range(3):
        _queue(mon, "i0", f"s{k}", 500)
    _queue(mon, "i0", "leg", 500, replaced=1)
    moves = sched.queue_replacements(slack_s=0.05, limit=10)
    assert moves, "the fresh legs in the same priced-out queue still move"
    assert all(rid != "leg" for rid, _, _ in moves)


def test_the_slack_is_the_move_hysteresis(tmp_path):
    """The peer prices an incoming leg at 0.96 s: inside budget by 0.03 - a
    move only a slack of 0.01 permits, and 0.05 refuses."""
    _, mon, sched = build(tmp_path=tmp_path)
    for k in range(3):
        _queue(mon, "i0", f"s{k}", 500)
    _queue(mon, "i0", "leg", 500)
    _queue(mon, "i1", "peer", 460)  # any leg re-priced at i1 reads 0.46 + 0.50
    assert sched.queue_replacements(slack_s=0.05, limit=10) == []
    moves = sched.queue_replacements(slack_s=0.01, limit=10)
    assert moves
    assert all(src == "i0" and dst == "i1" for _, src, dst in moves)


def test_an_ejected_peer_never_takes_a_move(tmp_path):
    """The breaker's view wins: no placement lands on a held-out instance."""
    clock, mon, sched = build(n_prefill=2, n_decode=1, tmp_path=tmp_path)
    for k in range(3):
        _queue(mon, "i0", f"s{k}", 500)
    _queue(mon, "i0", "leg", 500)
    sched.ejected["i1"] = clock()
    assert sched.queue_replacements(slack_s=0.05) == []


def test_decode_residency_is_not_a_replacement_candidate(tmp_path):
    """A decode leg's KV has migrated; only unstarted prefill is cheap to move."""
    _, mon, sched = build(tmp_path=tmp_path)
    mon.dispatched("i0", Request(rid="d1", input_len=500, phase=Phase.DECODE))
    assert sched.queue_replacements(slack_s=0.05) == []


def test_the_pass_cap_moves_the_deepest_queue_first(tmp_path):
    _, mon, sched = build(tmp_path=tmp_path)
    for k in range(6):
        _queue(mon, "i0", f"leg{k}", 500)
    moves = sched.queue_replacements(slack_s=0.05, limit=2)
    assert len(moves) == 2
    assert all(dst == "i1" for _, _, dst in moves)


def test_meets_slo_margin_widens_only_the_prefill_budget(tmp_path):
    _, _, sched = build(tmp_path=tmp_path)
    pre = Request(rid="p", input_len=500)
    assert not sched.meets_slo(pre, (0.0, 1.05))
    assert sched.meets_slo(pre, (0.0, 1.05), ttft_margin=0.1)
    dec = Request(rid="d", input_len=10, phase=Phase.DECODE)
    assert not sched.meets_slo(dec, (0.0, 0.01), ttft_margin=0.1)


def test_the_own_prefill_floor_drops_everything_that_drains(tmp_path):
    """The door splits its refusals on this number, so it has to hold only
    what stays: not the queue ahead, and not a probation penalty that lifts.
    """

    class _Probated:
        penalty_s = 5.0

        def probation_set(self) -> set[str]:
            return {"i0", "i1"}

    _, mon, sched = build(tmp_path=tmp_path)
    req = Request(rid="r", input_len=500)
    floor = sched.cheapest_own_prefill(req)
    assert floor == pytest.approx(0.5025)  # 1e-8 n^2 + 1e-3 n, alone
    assert sched.cheapest_prefill_price(req) == pytest.approx(floor)

    # A queue ahead of it on every prefill instance, then a probation penalty
    # on top. The quoted landing climbs with each; the floor does not move.
    for iid in ("i0", "i1"):
        mon.dispatched(iid, Request(rid=f"{iid}-ahead", input_len=500))
    assert sched.cheapest_prefill_price(req) == pytest.approx(2 * floor)
    assert sched.cheapest_own_prefill(req) == pytest.approx(floor)

    sched.health = _Probated()
    assert sched.cheapest_prefill_price(req) == pytest.approx(2 * floor + 5.0)
    assert sched.cheapest_own_prefill(req) == pytest.approx(floor)


# -- cooperative prefix reuse ----------------------------------------


def _coop(tmp_path, halflife: float = 1e9):
    """build() with the cooperative term on; a huge half-life pins warmth at 1."""
    clock, mon, sched = build(n_prefill=2, n_decode=2, tmp_path=tmp_path)
    sched.prefix_coop = True
    sched.prefix_halflife_s = halflife
    return clock, mon, sched


def test_a_warm_engine_wins_an_otherwise_exact_tie(tmp_path):
    """The cached prefix priced as a discount inside the cost, so reuse
    wins the tie fleet-first placement would otherwise break by accident."""
    _, mon, sched = _coop(tmp_path)
    sched.remember(Request(rid="w", input_len=200, prefix_key=7), mon.instances["i1"])

    chosen = sched.schedule(Request(rid="r", input_len=300, prefix_key=7))
    assert chosen.iid == "i1", "the warm engine wins the tie"

    _, _, cold = build(n_prefill=2, n_decode=2, tmp_path=tmp_path)
    cold.remember(Request(rid="w", input_len=200, prefix_key=7), cold.monitor.instances["i1"])
    assert cold.schedule(Request(rid="r", input_len=300, prefix_key=7)).iid == "i0", (
        "without the term the pairing is invisible and the first engine wins ties"
    )


def test_the_discount_is_exactly_the_cached_tokens_prefill_time(tmp_path):
    """T(300) - T(200): the saving is the part of the pass already computed."""
    _, mon, sched = _coop(tmp_path)
    sched.remember(Request(rid="w", input_len=200, prefix_key=7), mon.instances["i1"])
    r = Request(rid="r", input_len=300, prefix_key=7)
    profile = sched.profiles.get("i1")
    base = profile.prefill_time(300)
    cost = sched.cost(r, mon.instances["i1"])
    assert cost == (0.0, pytest.approx(base - profile.prefill_time(200)))


def test_a_longer_new_request_is_credited_only_for_the_cached_overlap(tmp_path):
    _, mon, sched = _coop(tmp_path)
    sched.remember(Request(rid="w", input_len=150, prefix_key=7), mon.instances["i1"])
    profile = sched.profiles.get("i1")
    cost = sched.cost(Request(rid="r", input_len=400, prefix_key=7), mon.instances["i1"])
    assert cost == (0.0, pytest.approx(profile.prefill_time(400) - profile.prefill_time(150)))


def test_the_warm_engine_loses_a_conflict_the_resident_work_prices_larger(tmp_path):
    """Never an override: once the queue ahead prices above the saving, the
    fleet-first answer stands."""
    _, mon, sched = _coop(tmp_path)
    sched.remember(Request(rid="w", input_len=200, prefix_key=7), mon.instances["i1"])
    mon.dispatched("i1", Request(rid="load", input_len=700))

    chosen = sched.schedule(Request(rid="r", input_len=300, prefix_key=7))
    assert chosen.iid == "i0", "resident 0.70s+own 0.30s-saving 0.20s loses to idle 0.30s"


def test_warmth_decays_on_the_half_life(tmp_path):
    """Engines evict silently; the credit fades rather than lies."""
    clock, mon, sched = _coop(tmp_path, halflife=10.0)
    sched.remember(Request(rid="w", input_len=200, prefix_key=7), mon.instances["i1"])
    clock.advance(10.0)
    profile = sched.profiles.get("i1")
    cost = sched.cost(Request(rid="r", input_len=300, prefix_key=7), mon.instances["i1"])
    assert cost == (0.0, pytest.approx(profile.prefill_time(300) - 0.5 * profile.prefill_time(200)))


def test_every_engine_the_key_has_landed_on_claims_the_discount(tmp_path):
    """The map is a set of holders, not one home: after the key has been
    computed on both engines, both price it warm. A pile has nothing to
    pile for - balance with the savings kept."""
    _, mon, sched = _coop(tmp_path)
    sched.remember(Request(rid="a", input_len=200, prefix_key=7), mon.instances["i0"])
    sched.remember(Request(rid="b", input_len=200, prefix_key=7), mon.instances["i1"])
    profile = sched.profiles.get("i0")
    r = Request(rid="r", input_len=300, prefix_key=7)
    assert sched.cost(r, mon.instances["i0"])[1] == pytest.approx(
        profile.prefill_time(300) - profile.prefill_time(200)
    )
    assert sched.cost(r, mon.instances["i1"])[1] == pytest.approx(
        profile.prefill_time(300) - profile.prefill_time(200)
    )


def test_an_ejected_engine_takes_its_warm_records_with_it(tmp_path):
    _, mon, sched = _coop(tmp_path)
    sched.remember(Request(rid="a", input_len=200, prefix_key=7), mon.instances["i0"])
    for _ in range(3):
        sched.record_failure("i0", connection_shaped=True)
    assert "i0" in sched.ejected
    assert sched._warm == {}, "the engine's records die with it"
    r = Request(rid="r", input_len=300, prefix_key=7)
    profile = sched.profiles.get("i1")
    assert sched.cost(r, mon.instances["i1"])[1] == pytest.approx(profile.prefill_time(300))


def test_the_term_is_invisible_when_off(tmp_path):
    _, mon, sched = build(n_prefill=2, n_decode=2, tmp_path=tmp_path)
    sched.remember(Request(rid="w", input_len=200, prefix_key=7), mon.instances["i1"])
    assert not sched._warm, "nothing recorded while the term is off"
    profile = sched.profiles.get("i1")
    cost = sched.cost(Request(rid="r", input_len=300, prefix_key=7), mon.instances["i1"])
    assert cost[1] == pytest.approx(profile.prefill_time(300))


def test_the_joint_matching_reads_the_same_discount(tmp_path):
    """A windowed assignment prices reuse too: each window's warm key goes to
    its engine or the matching is not reading the same costs."""
    _, mon, sched = _coop(tmp_path)
    sched.remember(Request(rid="wa", input_len=200, prefix_key=1), mon.instances["i0"])
    sched.remember(Request(rid="wb", input_len=200, prefix_key=2), mon.instances["i1"])
    a = Request(rid="a", input_len=300, prefix_key=1)
    b = Request(rid="b", input_len=300, prefix_key=2)
    placed = sched.schedule_batch([a, b])
    assert placed["a"].iid == "i0"
    assert placed["b"].iid == "i1"


def test_scheduling_alone_teaches_nothing(tmp_path):
    """Placement is a quote; dispatch is the lesson. A request the door then
    refuses computed nothing anywhere, so schedule() recording warmth would
    credit an engine with a cache it never earned (the poisoned record then
    underprices every later landing of the key)."""
    _, _, sched = _coop(tmp_path)
    r = Request(rid="q", input_len=200, prefix_key=7)
    chosen = sched.schedule(r)
    assert sched._warm == {}, "an unadmitted placement must not warm the map"
    sched.remember(r, chosen)
    assert (7, chosen.iid) in sched._warm, "dispatch is what teaches"


# -- role pinning ------------------------------------------------------


def _press_decode(clock, mon, sched):
    """Drive the Algorithm 2 expand trigger: every decode engine over TPOT."""
    for inst in mon.pool(Role.DECODE):
        r = Request(rid=f"d{inst.iid}", input_len=10, phase=Phase.DECODE)
        mon.dispatched(inst.iid, r)
        mon.first_token(inst.iid, r.rid)
        clock.advance(0.1)
        mon.output_token(inst.iid, r.rid)
    for _ in range(sched.th.sustained_intervals - 1):
        assert sched.monitoring_pass() is None


def test_a_pinned_engine_is_never_the_mover(tmp_path):
    """The trigger fires, the flip lands - on the unpinned engine."""
    clock, mon, sched = build(n_prefill=2, n_decode=2, tmp_path=tmp_path)
    sched.pinned = frozenset({"i0"})
    _press_decode(clock, mon, sched)
    flipped = sched.monitoring_pass()
    assert flipped is not None
    assert flipped.iid == "i1"
    assert mon.instances["i0"].role is Role.PREFILL


def test_an_all_pinned_source_pool_refuses_the_flip(tmp_path):
    clock, mon, sched = build(n_prefill=2, n_decode=2, tmp_path=tmp_path)
    sched.pinned = frozenset({"i0", "i1"})
    _press_decode(clock, mon, sched)
    assert sched.monitoring_pass() is None
    assert {i.iid for i in mon.pool(Role.PREFILL)} == {"i0", "i1"}


def test_the_prefill_floor_refuses_the_last_shrink(tmp_path):
    """min_prefill counts the whole live pool, so the floor holds even though
    no engine is pinned."""
    clock, mon, sched = build(n_prefill=2, n_decode=2, tmp_path=tmp_path)
    sched.min_prefill = 2
    _press_decode(clock, mon, sched)
    assert sched.monitoring_pass() is None
    assert len(mon.pool(Role.PREFILL)) == 2
