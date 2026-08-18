"""The preflight gates, and what they can and cannot see."""

from __future__ import annotations

from pathlib import Path

import pytest

from narwhal.check import Report, gate_consume
from narwhal.config import EngineSpec, FleetConfig
from narwhal.scheduler import SLO
from narwhal.types import Role


def _cfg(n: int = 4) -> FleetConfig:
    return FleetConfig(
        model="m",
        engines=[
            EngineSpec(iid=f"e{k}", url=f"http://127.0.0.1:{8101 + k}", role=Role.DECODE)
            for k in range(n)
        ],
        slo=SLO(ttft_s=10.0, tpot_s=0.125),
        profiles_path=Path("/tmp/none.json"),
    )


class FakeEngines:
    """Counts probes and stalls a chosen consumer a fraction of the time."""

    def __init__(self, stall: str | None = None, stall_every: int = 1) -> None:
        self.calls: list[tuple[str, str]] = []
        self.stall = stall
        self.stall_every = stall_every
        self._n = 0

    async def prefill(self, url, endpoint, body, headers):
        return {"remote_engine_id": url}

    async def decode(self, url, endpoint, body, headers, kv_params, **kw):
        dst = f"e{int(url.rsplit(':', 1)[1]) - 8101}"
        src = f"e{int(kv_params['remote_engine_id'].rsplit(':', 1)[1]) - 8101}"
        self.calls.append((src, dst))
        self._n += 1
        if dst == self.stall and self._n % self.stall_every == 0:
            return
        yield 'data: {"choices":[{"index":0,"text":"t"}]}'


async def _run(cfg, engines, *, mesh=True, repeats=1) -> Report:
    rep = Report()
    live = {s.iid for s in cfg.engines}
    handoffs = {s.iid: {"remote_engine_id": s.url} for s in cfg.engines}
    await gate_consume(cfg, live, handoffs, engines, rep, mesh, repeats)
    return rep


@pytest.mark.asyncio
async def test_the_default_covers_every_ordered_pair():
    cfg, eng = _cfg(4), FakeEngines()
    await _run(cfg, eng)
    assert len(eng.calls) == 4 * 3, "n*(n-1) ordered pairs"
    assert len(set(eng.calls)) == 12


@pytest.mark.asyncio
async def test_the_ring_covers_a_fraction_of_them():
    cfg, eng = _cfg(6), FakeEngines()
    await _run(cfg, eng, mesh=False)
    assert len(eng.calls) == 6, "a ring is n pairs of the n*(n-1) the router can use"
    assert ("e2", "e0") not in eng.calls, "every probed pair belongs to the ring"


@pytest.mark.asyncio
async def test_repeats_multiply_the_probes_per_pair():
    cfg, eng = _cfg(3), FakeEngines()
    await _run(cfg, eng, repeats=4)
    assert len(eng.calls) == 3 * 2 * 4


@pytest.mark.asyncio
async def test_an_intermittent_stall_is_caught_by_repeating():
    """One probe finds a 1-in-4 stall a quarter of the time. Four find it."""
    cfg = _cfg(3)
    once = FakeEngines(stall="e0", stall_every=4)
    await _run(cfg, once, repeats=1)

    many = FakeEngines(stall="e0", stall_every=4)
    rep = await _run(cfg, many, repeats=4)
    assert rep.failed, "repeating the probe surfaces the intermittent stall"
    assert any("produced no tokens" in f for f in rep.failed)


@pytest.mark.asyncio
async def test_a_pair_is_reported_once_however_many_probes_pass():
    cfg, eng = _cfg(3), FakeEngines()
    rep = await _run(cfg, eng, repeats=5)
    assert not rep.failed


# -- refusing a busy fleet (issue #5) -------------------------------------


@pytest.mark.asyncio
async def test_resident_work_marks_the_router_busy():
    """Resident beats completions: at a low rate with long generations a short
    window sees no completion while the router is fully occupied."""
    import httpx

    from narwhal import bench

    state = {"served": 100, "resident": {"e0": {"prefill": 0, "decode": 7}}}
    bench.httpx = httpx
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=state))
    orig = httpx.AsyncClient
    httpx.AsyncClient = lambda **kw: orig(transport=transport, timeout=8.0)
    try:
        assert await bench._already_driven("http://r", settle_s=0.0) == "7 requests resident"
    finally:
        httpx.AsyncClient = orig


@pytest.mark.asyncio
async def test_an_idle_router_is_not_mistaken_for_a_busy_one():
    import httpx

    from narwhal import bench

    state = {"served": 42, "resident": {"e0": {"prefill": 0, "decode": 0}}}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=state))
    orig = httpx.AsyncClient
    httpx.AsyncClient = lambda **kw: orig(transport=transport, timeout=8.0)
    try:
        assert await bench._already_driven("http://r", settle_s=0.0) == ""
    finally:
        httpx.AsyncClient = orig


def test_a_free_port_reads_free():
    import socket as _s

    from narwhal.cli import _port_in_use

    # Hold the port for the whole assertion. Binding without listening is what
    # "free" means to _port_in_use, which decides by connect(): with no listen
    # queue the connection is refused. Reading the port and then closing the
    # socket first was a race, because the port went back to the ephemeral pool
    # while the assertion still named it, and anything on the machine could
    # take it in that gap. A second test run is the likely taker, since
    # test_probe.py picks a port the same way and then serves on it.
    with _s.socket(_s.AF_INET, _s.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
        assert _port_in_use(free_port) is None


def test_a_held_port_is_named_rather_than_silently_bound():
    import socket as _s

    from narwhal.cli import _port_in_use

    with _s.socket(_s.AF_INET, _s.SOCK_STREAM) as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        port = held.getsockname()[1]
        assert _port_in_use(port) == "accepting connections"


# -- the trace has to be able to show adaptation paying -------------------


def test_the_trace_inverts_the_prefill_decode_ratio():
    """the study's methodology §B: the instantaneous optimal P/D ratio has to cross the
    static split's fixed ratio, or no scheduler can beat a fixed one."""
    from narwhal.trace import SEGMENTS

    p1, p2, _ = SEGMENTS

    def mid(band):
        return sum(band) / 2

    assert mid(p1[1]) / mid(p1[2]) > 100, "P1 is prefill-heavy: long in, very short out"
    assert mid(p2[2]) / mid(p2[1]) > 5, "P2 is decode-heavy: short in, long out"


def test_the_phases_are_long_relative_to_migration_cost():
    """§B again: "where phases are too short, adaptive may thrash and lose".
    Migration is a relabel gated by the cooldown."""
    from narwhal.scheduler import Thresholds
    from narwhal.trace import PHASE_SECONDS

    assert 20 * Thresholds().cooldown_s <= PHASE_SECONDS


def test_each_phase_carries_its_own_rate_multiplier():
    """The phases differ in capacity by more than an order of magnitude on a
    measured fleet, so one arrival rate cannot bring both near their knee."""
    from narwhal.trace import SEGMENTS, make_trace

    assert len({m for _, _, _, m in SEGMENTS}) > 1

    trace = make_trace(0.5, seed=7)
    third = SEGMENTS[0][0]
    p1 = [r for r in trace if r[0] < third]
    p2 = [r for r in trace if third <= r[0] < 2 * third]
    assert len(p1) > len(p2) * 3, "the prefill phase is driven harder"
    assert max(r[2] for r in p2) > 2000, "the decode phase generates long outputs"


def test_the_multipliers_put_both_binding_phases_near_their_knee():
    """Derived from the measured profile: the fleet sustains ~7.7 req/s of the
    prefill-bound phase and ~0.49 of the decode-bound one, so a single rate
    without multipliers leaves one of them at a few percent of its knee."""
    from narwhal.trace import SEGMENTS

    p1_mult, p2_mult = SEGMENTS[0][3], SEGMENTS[1][3]
    knee_ratio = 7.74 / 0.49
    assert 0.6 < (p1_mult / p2_mult) / knee_ratio < 1.6, "P1 should run near its own knee"


def test_the_bench_prompt_is_sized_in_tokens_not_characters():
    """Measured on the fleet: 4,560 repetitions of the filler gave 4,467 tokens.
    Sizing by characters with a chars-per-token guess ran 2.7x short, which made
    the prefill-heavy phase far lighter than the trace asked for."""
    from narwhal.trace import FILLER, TOKENS_PER_REPEAT, _prompt_of

    for want in (500, 4000, 12000):
        reps = _prompt_of(want).count(FILLER)
        got = reps * TOKENS_PER_REPEAT
        assert abs(got - want) / want < 0.05, f"{want} tokens asked, {got:.0f} built"

    # The old character-based sizing, for the record.
    old = max(1, int(12000 * 3.8 / len(FILLER))) * TOKENS_PER_REPEAT
    assert old < 12000 * 0.5, "the character-based build was less than half"


def test_the_harness_passes_the_phase_length_through():
    """Editing the default on the node does not survive a deploy, which
    replaces the tree. The sweep has to carry it."""
    import pathlib

    sh = pathlib.Path("tools/compare.sh").read_text()
    assert "PHASE_SECONDS" in sh
    assert "--phase-seconds" in sh, "narwhal-bench takes it; the harness must pass it"


def test_print_example_config_is_loadable(capsys, tmp_path):
    """The example the wheel ships must satisfy the loader it ships with."""
    from narwhal.check import main
    from narwhal.config import FleetConfig

    assert main(["--print-example-config"]) == 0
    text = capsys.readouterr().out
    path = tmp_path / "fleet.json"
    path.write_text(text)
    cfg = FleetConfig.load(path)
    assert cfg.model
    assert len(cfg.engines) == 6


def test_mesh_covers_every_ordered_pair_and_ring_only_the_ring():
    from narwhal.check import _pairs_of

    ids = ["a", "b", "c", "d"]
    mesh = _pairs_of(ids, ids, mesh=True)
    assert len(mesh) == 12
    assert len(set(mesh)) == 12
    assert all(a != b for a, b in mesh)
    ring = _pairs_of(ids, ids, mesh=False)
    assert ring == [("a", "b"), ("b", "c"), ("c", "d"), ("d", "a")]
    assert set(ring) < set(mesh), "the ring is 4 of the 12 pairs the router can use"


# -- presets: one directory per (hardware, model) pair -------------


def test_the_template_preset_loads():
    """presets/_template/fleet.json must satisfy the loader it ships with."""
    from narwhal.check import preset_fleet
    from narwhal.config import FleetConfig

    cfg = FleetConfig.load(preset_fleet("_template"))
    assert cfg.model.startswith("<"), "the template ships placeholders, never a real model"
    assert len(cfg.engines) == 4


def test_every_shipped_preset_loads():
    """A preset whose config the loader refuses is prose, not a preset."""
    from pathlib import Path

    from narwhal.config import FleetConfig

    root = Path(__file__).resolve().parents[1] / "presets"
    names = sorted(d.name for d in root.iterdir() if (d / "fleet.json").exists())
    assert names == sorted(set(names)), "preset directories are unique by construction"
    assert names, "ship at least one preset"
    for name in names:
        FleetConfig.load(root / name / "fleet.json")


def test_a_preset_name_matches_its_directory():
    from narwhal.check import preset_fleet

    with pytest.raises(ValueError, match="directory name"):
        preset_fleet("../config")


def test_an_unknown_preset_lists_what_exists():
    from narwhal.check import preset_fleet

    with pytest.raises(ValueError, match=r"available: .*_template"):
        preset_fleet("h100-llama")


def test_the_preset_cli_names_the_resolution_error(capsys):
    from narwhal.check import main

    with pytest.raises(SystemExit) as err:
        main(["--preset", "h100-llama"])
    assert err.value.code == 2
    assert "_template" in capsys.readouterr().err


def _slo_fixture(tmp_path, tpot_intercept: float):
    from narwhal.check import Report
    from narwhal.config import EngineSpec, FleetConfig
    from narwhal.profiler import Profile, ProfileStore
    from narwhal.scheduler import SLO

    cfg = FleetConfig(
        model="m",
        engines=[EngineSpec(iid="e0", url="http://e0")],
        slo=SLO(ttft_s=10.0, tpot_s=0.125),
    )
    store = ProfileStore(tmp_path / "profiles.json")
    store.put(
        Profile(
            iid="e0",
            ttft_a=2e-8,
            ttft_b=6e-5,
            ttft_c=0.005,
            tpot_slope=3e-6,
            tpot_intercept=tpot_intercept,
        )
    )
    return cfg, store, Report()


def test_gate_slo_fails_a_target_below_the_measured_floor(tmp_path):
    """A TPOT target at or under the zero-contention interval cannot be met."""
    from narwhal.check import gate_slo

    cfg, store, rep = _slo_fixture(tmp_path, tpot_intercept=0.200)
    gate_slo(cfg, store, rep)
    assert len(rep.failed) == 1
    assert "unreachable" in rep.failed[0]


def test_gate_slo_passes_a_reachable_target(tmp_path):
    from narwhal.check import gate_slo

    cfg, store, rep = _slo_fixture(tmp_path, tpot_intercept=0.012)
    gate_slo(cfg, store, rep)
    assert rep.failed == []


def test_gate_slo_skips_an_unprofiled_instance(tmp_path):
    """No profile is a `profile` gate failure, not a false slo verdict."""
    from narwhal.check import Report, gate_slo
    from narwhal.config import EngineSpec, FleetConfig
    from narwhal.profiler import ProfileStore
    from narwhal.scheduler import SLO

    cfg = FleetConfig(
        model="m",
        engines=[EngineSpec(iid="e0", url="http://e0")],
        slo=SLO(ttft_s=10.0, tpot_s=0.125),
    )
    rep = Report()
    gate_slo(cfg, ProfileStore(tmp_path / "profiles.json"), rep)
    assert rep.failed == []
    assert len(rep.skipped) == 1


# -- the pace gate ----------------------------------------------


def _pace_transport(delays: dict[str, float]):
    import asyncio

    import httpx

    async def handler(request: httpx.Request) -> httpx.Response:
        iid = f"e{int(request.url.port) - 8101}"
        await asyncio.sleep(delays.get(iid, 0.01))
        return httpx.Response(200, json={"usage": {"prompt_tokens": 4096}, "choices": []})

    return httpx.MockTransport(handler)


async def test_a_throttled_engine_fails_the_pace_gate():
    """/health 200 and \"auto\" perflevel say nothing about pace; the
    gate convicts on measured wall time against the fleet median."""
    from narwhal.check import gate_pace

    cfg = _cfg(4)
    rep = Report()
    await gate_pace(
        cfg,
        {s.iid for s in cfg.engines},
        rep,
        repeats=1,
        transport=_pace_transport({"e2": 0.2}),
    )
    assert any("e2 pace" in f and "throttled or degraded" in f for f in rep.failed)
    assert len(rep.failed) == 1, "the healthy engines pass"


async def test_a_uniform_fleet_passes_the_pace_gate():
    from narwhal.check import gate_pace

    cfg = _cfg(4)
    rep = Report()
    await gate_pace(
        cfg, {s.iid for s in cfg.engines}, rep, repeats=1, transport=_pace_transport({})
    )
    assert rep.failed == []


async def test_uniform_drift_is_caught_by_the_stored_profile(tmp_path):
    """Every engine equally slow beats the median check; each engine's own
    fit still convicts the whole fleet."""
    from narwhal.check import gate_pace
    from narwhal.profiler import Profile, ProfileStore

    cfg = _cfg(3)
    store = ProfileStore(tmp_path / "p.json")
    for spec in cfg.engines:
        store.put(
            Profile(
                iid=spec.iid,
                ttft_a=0.0,
                ttft_b=1e-6,
                ttft_c=0.0,
                tpot_slope=1e-6,
                tpot_intercept=0.01,
            )
        )
    rep = Report()
    await gate_pace(
        cfg,
        {s.iid for s in cfg.engines},
        rep,
        store=store,
        repeats=1,
        transport=_pace_transport({f"e{k}": 0.2 for k in range(3)}),
    )
    assert sum("no longer describes" in f for f in rep.failed) == 3


async def test_two_engines_skip_the_median_comparison():
    from narwhal.check import gate_pace

    cfg = _cfg(2)
    rep = Report()
    await gate_pace(
        cfg, {s.iid for s in cfg.engines}, rep, repeats=1, transport=_pace_transport({})
    )
    assert rep.failed == []
    assert any("needs 3 engines" in s for s in rep.skipped)


async def test_pace_returns_the_convicted_engines():
    """The KV gates must not probe through a degraded engine - a stalled
    transfer kills the healthy peer's engine core, so run() skips them on
    any pace failure. The gate reports who to skip for."""
    from narwhal.check import gate_pace

    cfg = _cfg(4)
    rep = Report()
    slow = await gate_pace(
        cfg,
        {s.iid for s in cfg.engines},
        rep,
        repeats=1,
        transport=_pace_transport({"e1": 0.2}),
    )
    assert slow == {"e1"}


# -- consume pairs respect role pins -----------------------------------


def test_a_pinned_prefill_engine_is_never_a_consumer():
    """Production never routes a crossed decode leg to a pinned-prefill
    engine, and probing that pair can kill a healthy
    peer - so the gate builds its pairs over the roles the config allows."""
    from narwhal.check import _pairs_of

    ids = [f"e{k}" for k in range(6)]
    producers = ids
    consumers = [i for i in ids if i != "e0"]  # e0 pinned prefill
    for shape in (True, False):
        pairs = _pairs_of(producers, consumers, shape)
        assert all(dst != "e0" for _, dst in pairs)
        assert {p for p, _ in pairs} == set(producers), "every producer still probes"
        assert {c for _, c in pairs} == set(consumers), "every consumer still probed"


def test_an_unpinned_fleet_keeps_the_exact_old_shapes():
    from narwhal.check import _pairs_of

    ids = [f"e{k}" for k in range(6)]
    assert _pairs_of(ids, ids, True) == [(a, b) for a in ids for b in ids if a != b]
    assert _pairs_of(ids, ids, False) == [(ids[k], ids[(k + 1) % 6]) for k in range(6)]
