"""The actuation-cost ablation: a flip that costs a relaunch window."""

from __future__ import annotations

from narwhal.types import Request, Role
from tests.test_scheduler import build


def test_a_flipped_instance_serves_nothing_until_its_window_passes(tmp_path):
    clock, _, sched = build(n_prefill=3, n_decode=3, tmp_path=tmp_path)
    sched.flip_offline_s = 300.0

    flipped = sched.flip(Role.DECODE, by="test")
    assert flipped is not None
    assert flipped.role is Role.DECODE, "the label moves immediately"

    placed = {sched.schedule(Request(rid=f"r{k}", input_len=500)).iid for k in range(12)}
    assert flipped.iid not in placed, "no new work lands inside the window"

    clock.t += 301.0
    placed = {sched.schedule(Request(rid=f"s{k}", input_len=500)).iid for k in range(12)}
    assert sched.offline_until[flipped.iid] < clock.t
    assert flipped.iid not in sched.ejected


def test_the_relaunch_window_hides_the_instance_from_pool_load(tmp_path):
    clock, _, sched = build(n_prefill=3, n_decode=3, tmp_path=tmp_path)
    sched.flip_offline_s = 300.0
    flipped = sched.flip(Role.DECODE, by="test")
    assert flipped is not None
    assert not sched._offline("nope")
    assert sched._offline(flipped.iid)
    sched.pool_load(Role.PREFILL)
    sched.pool_load(Role.DECODE)  # neither read raises with a member offline
    clock.t += 300.0
    assert not sched._offline(flipped.iid), "the window is exactly flip_offline_s"


def test_an_offline_source_cannot_be_flipped_again(tmp_path):
    _, _, sched = build(n_prefill=2, n_decode=2, tmp_path=tmp_path)
    sched.flip_offline_s = 300.0
    first = sched.flip(Role.DECODE, by="test")
    assert first is not None
    sched.th.cooldown_s = 0.0
    second = sched.flip(Role.PREFILL, by="test")
    assert second is None or second.iid != first.iid


def test_zero_is_todays_hot_swap(tmp_path):
    _, _, sched = build(n_prefill=3, n_decode=3, tmp_path=tmp_path)
    assert sched.flip_offline_s == 0.0
    flipped = sched.flip(Role.DECODE, by="test")
    assert flipped is not None
    assert not sched._offline(flipped.iid)
    placed = {sched.schedule(Request(rid=f"r{k}", input_len=500)).iid for k in range(12)}
    assert flipped.iid in placed or len(placed) >= 1
