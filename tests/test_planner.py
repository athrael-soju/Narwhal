"""The plan loop, held to the study that earned it.

Each test pins one behavior the sim race measured: the warmup-hold, the
absolute law, the asymmetric damping, the one-pass move, and the wiring
that keeps two controllers from fighting over one fleet.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from narwhal.monitor import InstanceMonitor
from narwhal.planner import Planner
from narwhal.profiler import Profile, ProfileStore
from narwhal.scheduler import SLO, GlobalScheduler, Thresholds
from narwhal.types import Instance, Phase, Request, Role


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def build(n_prefill: int = 3, n: int = 6):
    clock = Clock()
    store = ProfileStore(Path(tempfile.mkdtemp()) / "profiles.json")
    mon = InstanceMonitor(clock=clock, profiles=store)
    for k in range(n):
        iid = f"i{k}"
        mon.add(
            Instance(
                iid=iid, url=f"http://{iid}", role=Role.PREFILL if k < n_prefill else Role.DECODE
            )
        )
        store.put(
            Profile(
                iid=iid,
                ttft_a=2e-10,
                ttft_b=6.1e-5,
                ttft_c=0.069,
                tpot_slope=6.7e-7,
                tpot_intercept=0.021,
            )
        )
    sched = GlobalScheduler(mon, store, SLO(ttft_s=3.0, tpot_s=0.06), Thresholds(), clock=clock)
    planner = Planner(
        mon,
        sched,
        clock=clock,
        interval_s=15.0,
        window_s=30.0,
        confirmations=2,
        utilization=0.8,
        min_arrivals=10,
        demand_floor=0.5,
    )
    return clock, mon, sched, planner


def flood(planner, clock, n=40, input_len=14000, span=20.0):
    """Arrivals across `span` seconds ending at the clock's now."""
    start = clock.t - span
    for k in range(n):
        at = start + span * k / n
        planner._arrivals.append((at, input_len))


def test_warmup_hold_refuses_to_plan_on_thin_signal():
    """Dynamo's load_min_observations: a 0.2-instance reading slammed the
    study's fleet to 1P5D; below the floors the planner holds station."""
    clock, mon, _sched, planner = build()
    clock.t = 100.0
    planner._arrivals = [(99.0, 500)] * 3  # three small arrivals: thin
    assert planner._target(clock.t) is None
    assert len(mon.pool(Role.PREFILL)) == 3


def test_the_law_is_absolute_not_proportional():
    """A near-zero decode reading must not round the split to 5P1D when
    prefill's absolute need is four instances (the study's 20% failure)."""
    clock, _mon, _sched, planner = build()
    clock.t = 100.0
    flood(planner, clock, n=60, input_len=14000, span=30.0)  # ~2 req/s of 14k
    planner._residency = [(clock.t - 1, 0.1)]
    want = planner._target(clock.t)
    # prefill demand ~2 req/s * ~0.92 s = ~1.9 instances -> ceil(1.9/0.8) = 3
    assert want is None or want <= 4, "absolute law caps at the need, never the ratio"


def test_growing_a_starving_pool_acts_now():
    clock, _mon, _sched, planner = build(n_prefill=1)
    clock.t = 100.0
    flood(planner, clock, n=80, input_len=15000, span=30.0)  # heavy prefill, 1P pool
    planner._residency = [(clock.t - 1, 0.2)]
    want = planner._target(clock.t)
    assert want is not None, "under-provisioned prefill must not wait to confirm"
    assert want > 1


def test_pure_rebalancing_waits_for_confirmation():
    """Surplus shuffling is damped: same target twice before a move."""
    clock, _mon, _sched, planner = build(n_prefill=4)
    clock.t = 100.0
    flood(planner, clock, n=30, input_len=2000, span=30.0)  # light prefill
    planner._residency = [(clock.t - 1, 1.6)]  # decode wants ~2, has 2: not starving
    first = planner._target(clock.t)
    assert first is None, "first sighting only arms the confirmation"
    clock.t += 15.0
    flood(planner, clock, n=30, input_len=2000, span=15.0)
    planner._residency.append((clock.t - 1, 1.6))
    second = planner._target(clock.t)
    assert second is not None, "confirmed rebalance moves prefill down"
    assert second < 4


def test_moves_land_in_one_pass_and_on_the_flip_record():
    _clock, mon, sched, planner = build(n_prefill=5)
    moved = planner._move_to(2)
    assert moved == 3, "all needed instances move in one pass"
    assert len(mon.pool(Role.PREFILL)) == 2
    assert [f.by for f in sched.flips[-3:]] == ["planner"] * 3
    assert all(f.to is Role.DECODE for f in sched.flips[-3:])


def test_the_emptiest_instance_moves_first():
    _clock, mon, _sched, planner = build(n_prefill=3)
    mon.dispatched("i0", Request(rid="a", input_len=1000))
    mon.dispatched("i1", Request(rid="b", input_len=1000))
    planner._move_to(2)
    assert mon.instances["i2"].role is Role.DECODE, "the idle instance was the mover"


def test_an_ejected_instance_is_not_a_mover():
    _clock, mon, sched, planner = build(n_prefill=3)
    sched.ejected["i2"] = 0.0
    planner._move_to(2)
    assert mon.instances["i2"].role is Role.PREFILL, "ejected stays labeled, not moved"
    assert len(mon.pool(Role.PREFILL)) == 2


def test_the_scheduler_keeps_its_hands_off_under_a_planner():
    """Algorithm 1's step-3 inline flip is suppressed when the controller
    owns the pools: the naive-hybrid arm spent 388 moves on this fight."""
    _clock, mon, sched, _planner = build(n_prefill=1)
    sched.controller_owns_flips = True
    before = len(mon.pool(Role.PREFILL))
    # A decode request that meets no SLO reaches step 3 and would flip P->D.
    r = Request(rid="x", input_len=200_000, phase=Phase.DECODE)
    sched.schedule(r)
    assert len(mon.pool(Role.PREFILL)) == before, "no organic flip under the planner"
    assert len(sched.flips) == 0


def test_the_fast_loop_grows_a_starving_pool_between_plans():
    clock, mon, _sched, planner = build(n_prefill=1)
    clock.t = 100.0
    planner._last_fast = 90.0
    flood(planner, clock, n=80, input_len=15000, span=30.0)
    planner._residency = [(clock.t - 1, 0.2)]
    moved = planner.fast_step()
    assert moved == 1, "one step of starvation relief"
    assert len(mon.pool(Role.PREFILL)) == 2


def test_the_fast_loop_is_rate_capped():
    clock, _mon, _sched, planner = build(n_prefill=1)
    clock.t = 100.0
    planner._last_fast = 90.0
    flood(planner, clock, n=80, input_len=15000, span=30.0)
    planner._residency = [(clock.t - 1, 0.2)]
    assert planner.fast_step() == 1
    assert planner.fast_step() == 0, "a second step inside the cap is refused"
    clock.t += planner.fast_step_s
    assert planner.fast_step() == 1, "and allowed once the cap elapses"


def test_the_fast_loop_never_rebalances_surplus():
    """A pool merely above its need is the plan loop's business; the fast
    loop acts only on deficits, or the two loops fight (the 388-move
    lesson)."""
    clock, mon, _sched, planner = build(n_prefill=4)
    clock.t = 100.0
    planner._last_fast = 90.0
    flood(planner, clock, n=30, input_len=2000, span=30.0)  # light: needs ~1P
    planner._residency = [(clock.t - 1, 0.8)]  # decode need ~1, has 2: no deficit
    assert planner.fast_step() == 0, "surplus prefill is not the fast loop's problem"
    assert len(mon.pool(Role.PREFILL)) == 4


def test_the_fast_loop_respects_warmup():
    clock, _mon, _sched, planner = build(n_prefill=1)
    clock.t = 100.0
    planner._last_fast = 90.0
    planner._arrivals = [(99.0, 15000)] * 3  # under min_arrivals
    assert planner.fast_step() == 0


def test_a_mixed_fleet_is_priced_by_its_mean_not_its_first_profile(tmp_path):
    """Fleet-mean pricing: `any()` once priced the whole fleet through whichever engine
    profiled first. A fleet of one fast and one slow engine must price
    offered work between the two, not at either extreme."""
    store = ProfileStore(Path(tempfile.mkdtemp()) / "profiles.json")
    fast = Profile(
        iid="fast", ttft_a=0.0, ttft_b=1e-5, ttft_c=0.0, tpot_slope=0.0, tpot_intercept=0.02
    )
    slow = Profile(
        iid="slow", ttft_a=0.0, ttft_b=9e-5, ttft_c=0.0, tpot_slope=0.0, tpot_intercept=0.02
    )
    store.put(fast)
    store.put(slow)
    mean_t = store.mean_prefill_time(10_000)
    assert fast.prefill_time(10_000) < mean_t < slow.prefill_time(10_000)
    assert mean_t == (fast.prefill_time(10_000) + slow.prefill_time(10_000)) / 2

    mean_cap = store.mean_max_tokens(0.06)
    assert mean_cap == (fast.max_tokens(0.06) + slow.max_tokens(0.06)) / 2


def test_homogeneous_mean_equals_any(tmp_path):
    """The golden guarantee: on an identical-profile fleet the mean is
    byte-for-byte the single profile, so the study's numbers hold."""
    _, _, sched, _ = build()
    import pytest

    any_p = sched.profiles.any()
    assert sched.profiles.mean_prefill_time(14_000) == pytest.approx(
        any_p.prefill_time(14_000), rel=1e-12
    )
    assert sched.profiles.mean_max_tokens(0.06) == pytest.approx(any_p.max_tokens(0.06), rel=1e-12)


def test_a_pinned_engine_holds_its_seat_under_the_planner():
    _clock, mon, sched, planner = build(n_prefill=3)
    sched.pinned = frozenset({"i0"})
    moved = planner._move_to(1)
    assert moved == 2, "the plan lands, moving only the unpinned engines"
    assert mon.instances["i0"].role is Role.PREFILL
    assert {i.iid for i in mon.pool(Role.PREFILL)} == {"i0"}


def test_an_all_pinned_prefill_pool_cannot_shrink():
    _clock, mon, sched, planner = build(n_prefill=3)
    sched.pinned = frozenset({"i0", "i1", "i2"})
    assert planner._move_to(1) == 0
    assert len(mon.pool(Role.PREFILL)) == 3


def test_the_floor_binds_the_plan():
    _clock, mon, sched, planner = build(n_prefill=3)
    sched.min_prefill = 2
    planner._move_to(1)
    assert len(mon.pool(Role.PREFILL)) == 2, "want is clamped to the floor, not refused"


def test_ceil_wobble_at_a_boundary_waits_for_confirmation():
    """At a demand boundary the rounded need flips every window; a
    disagreement inside the deadband is a rebalance, not starvation, so it
    must survive `confirmations` consecutive plans before moving."""
    clock, mon, _sched, planner = build(n_prefill=4)
    # Demand that prices to need_p = 5 by ceil but only 4.2 raw: inside the
    # 0.5-engine deadband of the current 4.
    planner._demand = lambda now: (4.2 * planner.utilization, 1.0 * planner.utilization)
    planner._arrivals.extend([0.0] * planner.min_arrivals)
    clock.t += planner.interval_s
    assert planner.pass_due() == 0, "first sight of the boundary does not move"
    assert len(mon.pool(Role.PREFILL)) == 4
    clock.t += planner.interval_s
    assert planner.pass_due() == 1, "the second consecutive plan confirms and moves"


def test_a_real_phase_shift_still_acts_immediately():
    """A shortfall past the deadband is starvation; no confirmation tax."""
    clock, _mon, _sched, planner = build(n_prefill=2)
    planner._demand = lambda now: (4.8 * planner.utilization, 1.0 * planner.utilization)
    planner._arrivals.extend([0.0] * planner.min_arrivals)
    clock.t += planner.interval_s
    assert planner.pass_due() >= 1, "materially starving moves on first sight"
