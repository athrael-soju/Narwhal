"""Predictive health: drift instrumented per engine, against its own
profile, on a windowed cadence so a busy or idle minute cannot convict it."""

from __future__ import annotations

import json
import re

import pytest

from narwhal.config import FleetConfig
from narwhal.health import DriftTracker
from narwhal.monitor import InstanceMonitor
from narwhal.profiler import Profile, ProfileStore
from narwhal.scheduler import SLO, GlobalScheduler, Thresholds
from narwhal.types import Instance, Phase, Request, Role


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def build(n_prefill: int, n_decode: int, tmp_path, **health_kw):
    clock = FakeClock()
    store = ProfileStore(tmp_path / "profiles.json")
    mon = InstanceMonitor(clock=clock, profiles=store)
    for k in range(n_prefill + n_decode):
        iid = f"i{k}"
        mon.add(
            Instance(
                iid=iid, url=f"http://{iid}", role=Role.PREFILL if k < n_prefill else Role.DECODE
            )
        )
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
    health = DriftTracker(
        clock=clock,
        window_s=30.0,
        band=2.0,
        min_samples=3,
        probation_windows=3,
        evict_windows=5,
        recovery_windows=3,
        penalty_s=1.5,
        **health_kw,
    )
    sched = GlobalScheduler(
        mon, store, SLO(ttft_s=1.0, tpot_s=0.05), Thresholds(), clock=clock, health=health
    )
    sched._last_p2d_flip -= sched.th.cooldown_s
    return clock, mon, sched


def score_windows(tracker: DriftTracker, clock: FakeClock, iid: str, residual: float, n: int):
    """Tracker-only: `n` closed windows at a fixed residual. Verdicts returned."""
    out = []
    for _ in range(n):
        for _ in range(3):
            tracker.note(iid, residual)
        clock.advance(30.0)
        out.extend(tracker.tick())
    return out


def serve_windows(sched: GlobalScheduler, clock: FakeClock, iid: str, residual: float, n: int):
    """Integrated: samples land as the decode channel's per-pass readings do;
    the scheduler's monitoring pass closes the window and applies the verdict."""
    for _ in range(n):
        for _ in range(3):
            sched.health.note(iid, residual)
        clock.advance(30.0)
        sched.monitoring_pass()


# -- the tracker itself ---------------------------------------------------


def test_under_band_is_silent():
    clock = FakeClock()
    h = DriftTracker(clock=clock, window_s=30.0, min_samples=3, probation_windows=3)
    assert score_windows(h, clock, "i0", 1.2, 6) == []
    assert h.probation_set() == set()


def test_natural_cadence_is_not_drift():
    """A 1.0 residual - the engine ticking exactly at profile - never speaks."""
    clock = FakeClock()
    h = DriftTracker(clock=clock, window_s=30.0, min_samples=3, probation_windows=2)
    assert score_windows(h, clock, "i0", 1.0, 8) == []
    assert h.probation_set() == set()


def test_a_fleet_wide_surge_convicts_nobody():
    """Everyone at 2.6x together is capacity, not an engine story (the sim
    evidence for this guard came from exactly that false trail)."""
    clock = FakeClock()
    h = DriftTracker(clock=clock, window_s=30.0, min_samples=3, probation_windows=2)
    for _ in range(6):
        for iid in ("i0", "i1", "i2"):
            for _ in range(3):
                h.note(iid, 2.6)
        clock.advance(30.0)
        h.tick()
    assert h.probation_set() == set()


def test_a_solo_drifter_stands_out_from_the_surge():
    clock = FakeClock()
    h = DriftTracker(clock=clock, window_s=30.0, min_samples=3, probation_windows=2)
    for _ in range(3):
        for iid in ("i0", "i1"):
            for _ in range(3):
                h.note(iid, 1.1)
        for _ in range(3):
            h.note("i9", 4.0)
        clock.advance(30.0)
        h.tick()
    assert h.probation_set() == {"i9"}


def test_over_band_sustained_probates_on_the_third_window():
    clock = FakeClock()
    h = DriftTracker(clock=clock, window_s=30.0, min_samples=3, probation_windows=3)
    score_windows(h, clock, "i0", 1.0, 3)  # the healthy prelude seeds own history
    assert score_windows(h, clock, "i0", 3.0, 2) == []
    assert score_windows(h, clock, "i0", 3.0, 1) == [("probation", "i0")]
    assert h.probation_set() == {"i0"}


def test_a_window_short_of_samples_is_silence():
    clock = FakeClock()
    h = DriftTracker(clock=clock, window_s=30.0, min_samples=3, probation_windows=3)
    for _ in range(10):
        h.note("i0", 9.0)
        h.note("i0", 9.0)
        clock.advance(30.0)
        h.tick()
    assert h.probation_set() == set()


# -- the scheduler applies the verdicts ------------------------------------


def test_probation_then_sustained_drift_ejects(tmp_path):
    clock, _mon, sched = build(1, 2, tmp_path)
    serve_windows(sched, clock, "i1", 1.0, 3)
    serve_windows(sched, clock, "i1", 4.0, 3)
    assert "i1" in sched.health.probation_set()
    assert "i1" not in sched.ejected
    # Two more over-band windows reach evict_windows of sustained drift.
    serve_windows(sched, clock, "i1", 4.0, 2)
    assert "i1" in sched.ejected
    assert sched.health.probation_set() == set()


def test_recovery_clears_probation_in_place(tmp_path):
    clock, _mon, sched = build(1, 2, tmp_path)
    serve_windows(sched, clock, "i1", 1.0, 3)
    serve_windows(sched, clock, "i1", 4.0, 3)
    assert "i1" in sched.health.probation_set()
    serve_windows(sched, clock, "i1", 1.1, 3)
    assert sched.health.probation_set() == set()
    assert "i1" not in sched.ejected


def test_last_live_instance_is_never_drift_ejected(tmp_path):
    clock, _mon, sched = build(0, 1, tmp_path)
    serve_windows(sched, clock, "i0", 1.0, 3)
    serve_windows(sched, clock, "i0", 5.0, 9)
    # Nine windows of sustained drift still refuse to kill the last engine:
    # probation stands (the admission signal), ejection does not.
    assert sched.health.probation_set() == {"i0"}
    assert sched.ejected == {}


# -- what probation does to placement --------------------------------------


def test_probation_adds_the_flat_penalty_to_prefill_cost(tmp_path):
    clock, mon, sched = build(2, 1, tmp_path)
    req = Request(rid="r1", input_len=10, phase=Phase.PREFILL)
    before = sched.cost(req, mon.instances["i1"])[1]
    serve_windows(sched, clock, "i1", 1.0, 3)
    serve_windows(sched, clock, "i1", 4.0, 3)
    after = sched.cost(req, mon.instances["i1"])[1]
    assert after - before == pytest.approx(1.5)


def test_probated_engine_loses_the_argmin_to_a_peer(tmp_path):
    clock, _mon, sched = build(2, 1, tmp_path)
    serve_windows(sched, clock, "i0", 1.0, 3)
    serve_windows(sched, clock, "i0", 4.0, 3)
    assert sched.health.probation_set() == {"i0"}
    chosen = sched.schedule(Request(rid="c", input_len=10, phase=Phase.PREFILL))
    assert chosen.iid == "i1"


def test_note_ttft_records_the_ratio(tmp_path):
    _clock, _mon, sched = build(1, 2, tmp_path)
    assert "i1" not in sched.health._engines
    sched.note_ttft("i1", observed_s=0.6, predicted_s=0.5)
    engine = sched.health._engines["i1"]
    assert len(engine.ttft_residuals) == 1
    assert engine.ttft_residuals[0] == pytest.approx(1.2)
    assert engine.residuals == __import__("collections").deque() or len(engine.residuals) == 0


def test_the_small_signal_floor_keeps_voxel_noise_out(tmp_path):
    _clock, _mon, sched = build(1, 2, tmp_path)
    sched.note_ttft("i1", observed_s=0.02, predicted_s=0.01)  # aliased x2, under the floor
    assert "i1" not in sched.health._engines


# -- the config surface ----------------------------------------------------


def _fleet_json(tmp_path, health=None):
    cfg = {
        "model": "m",
        "engines": [{"iid": "i0", "url": "http://i0"}],
        "slo": {"ttft_s": 3.0, "tpot_s": 0.06},
    }
    if health is not None:
        cfg["health"] = health
    path = tmp_path / "fleet.json"
    path.write_text(json.dumps(cfg))
    return path


def test_health_block_is_a_known_key_and_roundtrips(tmp_path):
    cfg = FleetConfig.load(_fleet_json(tmp_path, health={"drift_band": 2.5, "window_s": 20.0}))
    assert cfg.health_drift_band == 2.5
    assert cfg.health_window_s == 20.0
    out = tmp_path / "roundtrip.json"
    cfg.save(out)
    loaded = FleetConfig.load(out)
    assert loaded.health_drift_band == 2.5
    assert loaded.health_min_samples == 3


def test_health_defaults_when_the_block_is_absent(tmp_path):
    cfg = FleetConfig.load(_fleet_json(tmp_path))
    assert cfg.health_drift_band == 2.0
    assert cfg.health_probation_windows == 3
    assert cfg.health_probation_penalty_s == 1.5


@pytest.mark.parametrize(
    ("health", "fragment"),
    [
        ({"drift_band": 1.0}, "health.drift_band must exceed 1.0"),
        ({"drift_band": 0.5}, "health.drift_band must exceed 1.0"),
        ({"probation_windows": 0}, "health.probation_windows must be at least 1"),
        ({"probation_penalty_s": -1.0}, "health.probation_penalty_s cannot be negative"),
    ],
)
def test_health_invalid_values_are_refused(tmp_path, health, fragment):
    with pytest.raises(ValueError, match=re.escape(fragment)):
        FleetConfig.load(_fleet_json(tmp_path, health=health))


# -- the review fixes: verdict-state and controller-path regressions -------


def test_a_vetoed_surge_window_does_not_ride_an_engine_out_of_probation(tmp_path):
    """A still-drifting engine must not recover through windows the surge
    guard vetoed: a vetoed window is evidence of nothing, either way."""
    clock = FakeClock()
    h = DriftTracker(clock=clock, window_s=30.0, min_samples=3, probation_windows=2)
    # Probation earned while the fleet is quiet (peers noted first so the
    # drifter's cold baseline seeds from their median, not its own drift).
    for _ in range(2):
        for iid in ("i0", "i1"):
            for _ in range(3):
                h.note(iid, 1.0)
        for _ in range(3):
            h.note("i9", 4.0)
        clock.advance(30.0)
        h.tick()
    assert h.probation_set() == {"i9"}
    # The whole fleet now surges to a comparable level for many windows:
    # i9's windows are vetoed, and its probation must stand.
    for _ in range(6):
        for iid in ("i0", "i1", "i9"):
            for _ in range(3):
                h.note(iid, 4.0)
        clock.advance(30.0)
        h.tick()
    assert h.probation_set() == {"i9"}


def test_a_refused_eviction_keeps_the_probation_penalty(tmp_path):
    """The tracker's evict verdict is a request; when the scheduler refuses
    it (last live instance) the engine must stay probated and priced."""
    clock, _, sched = build(1, 0, tmp_path)
    serve_windows(sched, clock, "i0", 1.0, 3)  # the healthy prelude seeds history
    serve_windows(sched, clock, "i0", 4.0, 12)
    assert "i0" not in sched.ejected, "the last live instance is never ejected"
    assert sched.health.probation_set() == {"i0"}, "probation stands with the refusal"


def test_a_confirmed_eviction_retires_the_record(tmp_path):
    """After a real ejection the engine re-earns its case from fresh
    windows; the dead record must not convict it on readmission."""
    clock, _, sched = build(2, 2, tmp_path)
    serve_windows(sched, clock, "i0", 1.0, 3)  # the healthy prelude seeds history
    for _ in range(12):
        serve_windows(sched, clock, "i0", 4.0, 1)
        if "i0" in sched.ejected:
            break
    assert "i0" in sched.ejected
    assert "i0" not in sched.health.probation_set()
    assert sched.health.score("i0") is None, "the record died with the ejection"


def test_an_extreme_drifter_speaks_through_the_surge_veto(tmp_path):
    """relative_band's magnitude: a score past relative_band x the scored
    peers' median is its own story even while the fleet surges."""
    clock = FakeClock()
    h = DriftTracker(
        clock=clock, window_s=30.0, min_samples=3, probation_windows=2, relative_band=1.5
    )
    for _ in range(4):
        for iid in ("i0", "i1"):
            for _ in range(3):
                h.note(iid, 2.6)  # a genuine fleet-wide surge...
        for _ in range(3):
            h.note("i9", 9.0)  # ...and one engine far beyond even that
        clock.advance(30.0)
        h.tick()
    assert "i9" in h.probation_set(), "9.0x against a 2.6x surge is not the surge"
    assert not {"i0", "i1"} & h.probation_set(), "the surge itself stays vetoed"
