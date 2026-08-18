"""The failover itself, in the suite: what a restart owes its replacement,
how the handoff is taken, and the refusals that keep a bad one out."""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import AsyncIterator
from dataclasses import asdict
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from narwhal import state as handoff_state
from narwhal.app import create_app
from narwhal.config import EngineSpec, FleetConfig
from narwhal.journal import RunJournal
from narwhal.profiler import Profile
from narwhal.scheduler import SLO, Thresholds
from narwhal.server import ArrowRouter, _monitor_once
from narwhal.types import Role


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _cfg(tmp_path: Path, **over) -> FleetConfig:
    cfg = FleetConfig(
        model="stub-model",
        engines=[
            EngineSpec(
                iid=f"e{k}",
                url=f"http://127.0.0.1:{8101 + k}",
                role=Role.PREFILL if k < 2 else Role.DECODE,
            )
            for k in range(4)
        ],
        slo=SLO(ttft_s=10.0, tpot_s=0.125),
        thresholds=Thresholds(expand=1.0, shrink=0.5, cooldown_s=10.0, sustained_intervals=3),
        profiles_path=tmp_path / "profiles.json",
        monitor_interval_s=60.0,
        tokenize=False,
        controller="reactive",
        state_path=tmp_path / "state.json",
    )
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def _profiles(cfg: FleetConfig) -> None:
    cfg.profiles_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.profiles_path.write_text(
        json.dumps(
            [
                asdict(
                    Profile(
                        iid=spec.iid,
                        ttft_a=2e-8,
                        ttft_b=6e-5,
                        ttft_c=0.005,
                        tpot_slope=3e-6,
                        tpot_intercept=0.012,
                    )
                )
                for spec in cfg.engines
            ]
        )
    )


def _router(tmp_path: Path, clock) -> ArrowRouter:
    async def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    cfg = _cfg(tmp_path)
    _profiles(cfg)
    journal = RunJournal(path=tmp_path / "journal.jsonl")
    journal.open()
    return ArrowRouter(cfg, journal, transport=httpx.MockTransport(handle), clock=clock)


def _scarred(clock, router: ArrowRouter) -> None:
    """Leave the visible marks a long run leaves: moved roles, a held engine,
    a relaunch window, counters that tell the afternoon's story."""
    router.monitor.instances["e2"].role = Role.PREFILL
    for _ in range(router.cfg.eject_after):
        router.scheduler.record_failure("e0", connection_shaped=True)
    assert "e0" in router.scheduler.ejected
    router.scheduler.offline_until["e1"] = clock() + 30.0
    router.served = 7
    router.failed = 1
    router.scheduler.unserved = 12


def test_roundtrip_restores_the_actuated_picture(tmp_path):
    clock_a = FakeClock()
    router_a = _router(tmp_path, clock_a)
    _scarred(clock_a, router_a)
    doc = handoff_state.snapshot(router_a)

    clock_b = FakeClock()
    clock_b.t = 5000.0
    router_b = _router(tmp_path, clock_b)
    report = handoff_state.apply(router_b, doc)

    assert report.applied
    assert report.roles_applied == 4
    assert router_b.monitor.instances["e2"].role is Role.PREFILL
    assert router_b.monitor.instances["e1"].role is Role.PREFILL
    assert sorted(router_b.scheduler.ejected) == ["e0"]
    assert router_b.scheduler.offline_until["e1"] == pytest.approx(5030.0)
    assert (router_b.served, router_b.failed, router_b.scheduler.unserved) == (7, 1, 12)
    assert report.gap_s is not None
    assert report.gap_s >= 0.0


def test_ejected_engine_is_due_for_a_probe_at_once(tmp_path):
    clock_a = FakeClock()
    router_a = _router(tmp_path, clock_a)
    _scarred(clock_a, router_a)
    doc = handoff_state.snapshot(router_a)

    clock_b = FakeClock()
    clock_b.t = 9000.0
    router_b = _router(tmp_path, clock_b)
    handoff_state.apply(router_b, doc)
    assert router_b.scheduler.probe_due(600.0) == ["e0"]


def test_apply_refuses_a_handoff_from_a_different_fleet(tmp_path):
    clock_a = FakeClock()
    router_a = _router(tmp_path, clock_a)
    _scarred(clock_a, router_a)
    doc = handoff_state.snapshot(router_a)
    doc["engines"] = ["elsewhere"]

    router_b = _router(tmp_path, FakeClock())
    report = handoff_state.apply(router_b, doc)
    assert not report.applied
    assert "this fleet is" in report.why
    assert router_b.monitor.instances["e2"].role is Role.DECODE
    assert router_b.scheduler.ejected == {}


def test_an_all_ejected_handoff_lands_as_none(tmp_path):
    clock_a = FakeClock()
    router_a = _router(tmp_path, clock_a)
    doc = handoff_state.snapshot(router_a)
    doc["ejected"] = ["e0", "e1", "e2", "e3"]

    router_b = _router(tmp_path, FakeClock())
    report = handoff_state.apply(router_b, doc)
    assert report.applied
    assert router_b.scheduler.ejected == {}


def test_load_is_none_for_missing_or_torn_files(tmp_path):
    assert handoff_state.load(tmp_path / "absent.json") is None
    torn = tmp_path / "state.json"
    torn.write_text('{"version": 1,')
    assert handoff_state.load(torn) is None


def test_write_overwrite_protocol_never_tears(tmp_path):
    path = tmp_path / "state.json"
    handoff_state.write(path, {"version": 1, "at": 1.0})
    handoff_state.write(path, {"version": 1, "at": 2.0})
    assert json.loads(path.read_text())["at"] == 2.0
    assert list(tmp_path.glob("*.tmp")) == []


@contextlib.asynccontextmanager
async def _started(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://router") as client,
    ):
        yield client


async def test_clean_shutdown_hands_down_and_resume_takes(tmp_path):
    """The failover itself: app A scars, shuts down, app B resumes its picture."""
    cfg_a = _cfg(tmp_path)
    _profiles(cfg_a)
    app_a = create_app(cfg_a, journal_path=tmp_path / "journal.jsonl")
    async with _started(app_a) as client:
        router_a: ArrowRouter = app_a.state.router
        router_a.monitor.instances["e2"].role = Role.PREFILL
        router_a.served = 4
        r = await client.get("/arrow/state")
        assert r.json()["served"] == 4

    assert (tmp_path / "state.json").exists()

    cfg_b = _cfg(tmp_path, resume=True)
    app_b = create_app(cfg_b, journal_path=tmp_path / "journal.jsonl")
    async with _started(app_b) as client:
        r = await client.get("/arrow/state")
    picture = r.json()
    assert picture["served"] == 4
    assert picture["pools"]["prefill"] == ["e0", "e1", "e2"]


async def test_monitor_pass_rewrites_the_handoff(tmp_path):
    router = _router(tmp_path, FakeClock())
    assert not router.cfg.state_path.exists()
    await _monitor_once(router)
    doc = handoff_state.load(router.cfg.state_path)
    assert doc is not None
    assert doc["run"] == router.journal.run
    before = doc["at"]
    time.sleep(0.02)
    await _monitor_once(router)
    assert handoff_state.load(router.cfg.state_path)["at"] > before


def test_the_handoff_never_moves_a_pinned_role(tmp_path):
    """A pinned engine's role is configuration, not actuated state: resume
    must not override it, even from a snapshot that says otherwise."""

    async def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    cfg = _cfg(tmp_path)
    cfg.engines[0].pin = True  # e0 opens PREFILL
    cfg.min_prefill = 2
    _profiles(cfg)
    journal = RunJournal(path=tmp_path / "journal.jsonl")
    journal.open()
    router = ArrowRouter(cfg, journal, transport=httpx.MockTransport(handle), clock=FakeClock())
    assert router.scheduler.pinned == frozenset({"e0"}), "the config's pin reaches the scheduler"
    assert router.scheduler.min_prefill == 2

    doc = handoff_state.snapshot(router)
    doc["roles"]["e0"] = "decode"  # a stale or hand-edited handoff
    doc["roles"]["e2"] = "prefill"
    report = handoff_state.apply(router, doc)
    assert report.applied
    assert router.monitor.instances["e0"].role is Role.PREFILL, "the pin outranks the handoff"
    assert router.monitor.instances["e2"].role is Role.PREFILL, "unpinned roles still land"
