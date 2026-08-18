"""The closed loop: observed misses trump the demand model."""

from __future__ import annotations

from narwhal.types import Role
from tests.test_planner import build, flood


def misses(sched, clock, n=40, ttft_ok=False, tpot_ok=True, span=20.0):
    start = clock.t - span
    for k in range(n):
        sched.outcomes.append((start + span * k / n, ttft_ok, tpot_ok))


def test_a_missing_window_escalates_prefill_past_the_model(tmp_path):
    """Demand arithmetic content with the current pool while requests
    blow TTFT: one escalation step, model overruled."""
    clock, _, sched, planner = build()
    clock.t = 100.0
    flood(planner, clock)  # demand the model prices as ~satisfied at 3P
    misses(sched, clock, ttft_ok=False)
    want = planner._target(clock.t)
    assert want == 4, f"one step past the current 3P, got {want}"


def test_a_healthy_window_leaves_the_model_in_charge(tmp_path):
    clock, _, sched, planner = build()
    clock.t = 100.0
    flood(planner, clock)
    misses(sched, clock, ttft_ok=True, tpot_ok=True)
    want = planner._target(clock.t)
    assert want != 4 or planner._hold_until == 0.0, "no escalation on a met floor"


def test_tpot_misses_escalate_decode_not_prefill(tmp_path):
    clock, _, sched, planner = build()
    clock.t = 100.0
    flood(planner, clock)
    misses(sched, clock, ttft_ok=True, tpot_ok=False)
    want = planner._target(clock.t)
    assert want == 2, f"one prefill instance surrendered to decode, got {want}"


def test_the_hold_blocks_the_models_relapse(tmp_path):
    """After an escalation the demand model still believes less is enough;
    the hold keeps the escalated size until the window turns healthy."""
    clock, mon, sched, planner = build()
    clock.t = 100.0
    flood(planner, clock)
    misses(sched, clock, ttft_ok=False)
    assert planner._target(clock.t) == 4
    for i in mon.pool(Role.DECODE)[:1]:
        i.role = Role.PREFILL  # actuate the escalation: 4P2D
    clock.t += 16.0
    flood(planner, clock)
    sched.outcomes.clear()
    misses(sched, clock, ttft_ok=True, tpot_ok=True)  # healthy now
    want = planner._target(clock.t)
    assert want is None or want >= 4, f"hold violated: model pulled back to {want}"


def test_a_zero_floor_disables_the_loop(tmp_path):
    clock, _, sched, planner = build()
    planner.attainment_floor = 0.0
    clock.t = 100.0
    flood(planner, clock)
    misses(sched, clock, ttft_ok=False)
    want = planner._target(clock.t)
    assert want != 4 or planner._hold_until == 0.0
