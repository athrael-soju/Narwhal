"""The drift tracker's ahead-condition, played in the simulator: an engine drifts four-fold
mid-run while the workload holds, and the predictive loop names and isolates
it before the reactive breaker ever fires."""

from __future__ import annotations

from pathlib import Path

from narwhal.health import DriftTracker
from narwhal.monitor import InstanceMonitor
from narwhal.profiler import Profile, ProfileStore
from narwhal.scheduler import SLO, GlobalScheduler, Thresholds
from narwhal.sim import Fleet, TraceEntry
from narwhal.types import Instance, Phase, Request, Role

TTFT_SLO = 10.0
TPOT_SLO = 0.125
PROFILE = Profile(
    iid="",
    ttft_a=2e-8,
    ttft_b=6e-5,
    ttft_c=0.005,
    tpot_slope=3e-6,
    tpot_intercept=0.012,
)


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _run_scenario(tmp_path: Path, predictive: bool):
    clock = FakeClock()
    mon = InstanceMonitor(clock=clock)
    store = ProfileStore(tmp_path / ("on" if predictive else "off") / "profiles.json")
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
    health = (
        DriftTracker(
            clock=clock,
            window_s=10.0,
            band=1.5,
            min_samples=2,
            probation_windows=3,
            evict_windows=5,
            recovery_windows=3,
            penalty_s=1.5,
        )
        if predictive
        else None
    )
    sched = GlobalScheduler(
        mon, store, SLO(ttft_s=TTFT_SLO, tpot_s=TPOT_SLO), Thresholds(), clock=clock, health=health
    )
    sched._last_p2d_flip -= sched.th.cooldown_s
    fleet = Fleet(mon, store, clock, kv_transfer_s=0.05, dt=0.01)

    arrivals = [t * 0.6 for t in range(150)]  # a P2-shaped walk: 800 in, 60 out
    predicted_ttft: dict[str, float] = {}
    next_free = 0
    last_pass = 0.0
    degraded = False
    steps = 0
    while steps < 120_000 and (next_free < len(arrivals) or fleet.awaiting_decode or fleet.live):
        if not degraded and clock() >= 30.0:
            # The residual-state event: i3 runs four times slower than profiled,
            # with no failure in sight - the signal health is built for.
            fleet.degradation["i3"] = 4.0
            degraded = True
        while next_free < len(arrivals) and arrivals[next_free] <= clock():
            entry = TraceEntry(
                at=arrivals[next_free], rid=f"r{next_free}", input_len=100, output_len=300
            )
            req = Request(rid=entry.rid, input_len=entry.input_len, phase=Phase.PREFILL)
            inst = sched.schedule(req)
            predicted_ttft[req.rid] = sched.cost(req, inst)[1]
            fleet.admit(entry, inst.iid)
            next_free += 1
        for rid in list(fleet.awaiting_decode):
            fleet.awaiting_decode.remove(rid)
            live = fleet.live[rid]
            sched.note_ttft(
                live.request.prefill_instance,
                live.first_token_at - live.arrived,
                predicted_ttft.pop(rid, 0.0),
            )
            live.request.phase = Phase.DECODE
            deployment = sched.schedule(live.request)
            fleet.dispatch_decode(rid, deployment.iid)
        fleet.step()
        if clock() - last_pass >= 1.0:
            last_pass = clock()
            sched.monitoring_pass()
        clock.advance(0.05)
        steps += 1
        if (
            clock() >= 130.0
            and not fleet.awaiting_decode
            and all(live.finished_at is not None for live in fleet.live.values())
        ):
            break
    return fleet, sched, degraded


# Keep finished live rows: the metric reads them.


def test_the_loop_names_and_isolates_the_drifting_engine(tmp_path):
    """The drift tracker's offer, played in the simulator: after t=30 engine i3 decodes at
    4x, and the tracker - with a fleet to compare against and its own history
    to seed from - probates and evicts exactly that engine."""
    _fleet, sched, degraded = _run_scenario(tmp_path, predictive=True)
    assert degraded
    assert sorted(sched.ejected) == ["i3"]
    assert sched.health.probation_set() == set()
    assert sched.health.score("i2") is not None  # the peers stayed attestable


def test_the_peers_carrying_the_shared_load_are_left_alone(tmp_path):
    """Engines that take the ejected engine's share rise together - a
    capacity story, not an engine story - and never get convicted for it."""
    _fleet, sched, _degraded = _run_scenario(tmp_path, predictive=True)
    assert set(sched.ejected) == {"i3"}
    for iid in ("i0", "i1", "i2"):
        e = sched.health._engines.get(iid)
        if e is None:
            continue  # never scored: no charge to answer
        assert not e.on_probation, f"{iid} should never have been drifty"


def test_without_health_the_drifting_engine_keeps_its_share(tmp_path):
    """The reactive baseline: nobody ejects, the degraded engine keeps being
    placed-on, and attainment still ends at one - the load stays affordable
    here; it is the queue residence time that quietly bleeds."""
    fleet, sched, _degraded = _run_scenario(tmp_path, predictive=False)
    assert set(sched.ejected) == set()
    frac, met, total = fleet.attainment(TTFT_SLO, TPOT_SLO)
    assert total == 150
    assert met == total
    assert frac == 1.0  # baseline was not penalized either


def test_attainment_holds_through_the_eviction(tmp_path):
    fleet_on, _s1, _d1 = _run_scenario(tmp_path, predictive=True)
    fleet_off, _s2, _d2 = _run_scenario(tmp_path, predictive=False)
    frac_on = fleet_on.attainment(TTFT_SLO, TPOT_SLO)[0]
    frac_off = fleet_off.attainment(TTFT_SLO, TPOT_SLO)[0]
    assert frac_on >= frac_off
