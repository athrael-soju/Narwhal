"""The warm standby, and the takeover the suite must hold."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import asdict
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from narwhal.app import create_app
from narwhal.config import EngineSpec, FleetConfig
from narwhal.profiler import Profile
from narwhal.scheduler import SLO, Thresholds
from narwhal.types import Role


def _cfg(tmp_path: Path, name: str) -> FleetConfig:
    return FleetConfig(
        model="stub-model",
        engines=[
            EngineSpec(
                iid=f"e{k}",
                url=f"http://127.0.0.1:{8151 + k}",
                role=Role.PREFILL if k < 2 else Role.DECODE,
            )
            for k in range(4)
        ],
        slo=SLO(ttft_s=10.0, tpot_s=0.125),
        thresholds=Thresholds(expand=1.0, shrink=0.5, cooldown_s=10.0, sustained_intervals=3),
        profiles_path=tmp_path / "profiles.json",
        monitor_interval_s=60.0,
        tokenize=False,
        state_path=tmp_path / f"state.{name}.json",
    )


def _profiles(cfg: FleetConfig) -> None:
    rows = [
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
    cfg.profiles_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.profiles_path.write_text(json.dumps(rows))


@contextlib.asynccontextmanager
async def _started(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://router") as client,
    ):
        yield client


class _Switchable(httpx.AsyncBaseTransport):
    """Routes to the primary's ASGI interface until the kill switch flips."""

    def __init__(self, app: FastAPI) -> None:
        self._inner = httpx.ASGITransport(app=app)
        self.dead = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.dead:
            raise httpx.ConnectError("primary is down")
        return await self._inner.handle_async_request(request)


async def test_a_standby_holds_the_door_and_says_so(tmp_path):
    """503 with Retry-After while the primary answers; /health names the state."""
    cfg = _cfg(tmp_path, "standby")
    _profiles(cfg)
    app = create_app(
        cfg,
        journal_path=tmp_path / "sb.jsonl",
        standby_of="http://primary",
        # An interval far longer than the test: the watch never concludes.
        standby_probe_interval_s=30.0,
    )
    async with _started(app) as client:
        health = (await client.get("/health")).json()
        assert health["status"] == "standby"
        r = await client.post("/v1/completions", json={"model": "stub-model", "prompt": "x"})
        assert r.status_code == 503
        assert r.headers["retry-after"] == "1"
        assert r.json()["error"]["code"] == "standby"


async def test_the_handoff_endpoint_serves_the_live_document(tmp_path):
    cfg = _cfg(tmp_path, "primary")
    _profiles(cfg)
    app = create_app(cfg, journal_path=tmp_path / "p.jsonl")
    async with _started(app) as client:
        doc = (await client.get("/arrow/handoff")).json()
    assert doc["engines"] == ["e0", "e1", "e2", "e3"]
    assert doc["roles"]["e0"] == "prefill"
    assert set(doc["counters"]) >= {"served", "failed", "unserved"}


async def test_takeover_applies_the_freshest_handoff_and_opens_the_door(tmp_path):
    """The failover itself, in the suite: the
    standby shadows the primary's moved roles and continued counters, takes
    over within a second of silence, and serves."""
    pcfg = _cfg(tmp_path, "primary")
    scfg = _cfg(tmp_path, "standby")
    _profiles(pcfg)
    primary = create_app(pcfg, journal_path=tmp_path / "p.jsonl")
    # The picture the standby must inherit: a moved role and spoken counters.
    primary.state.router.monitor.instances["e0"].role = Role.DECODE
    primary.state.router.served = 7
    primary.state.router.failed = 1
    switch = _Switchable(primary)
    standby = create_app(
        scfg,
        journal_path=tmp_path / "s.jsonl",
        standby_of="http://primary",
        standby_probe_interval_s=0.02,
        standby_takeover_after=2,
        standby_transport=switch,
    )
    async with _started(primary), _started(standby) as sclient:
        for _ in range(50):  # let a few polls land
            await asyncio.sleep(0.02)
            if (scfg.state_path).exists():
                break
        assert (await sclient.get("/health")).json()["status"] == "standby"

        switch.dead = True
        deadline = asyncio.get_event_loop().time() + 1.0
        while asyncio.get_event_loop().time() < deadline:
            if (await sclient.get("/health")).json()["status"] == "ok":
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("standby did not take over within a second of silence")

        router = standby.state.router
        assert router.monitor.instances["e0"].role is Role.DECODE, "the moved role continued"
        assert router.served == 7, "the counters continue rather than restart"
        assert router.failed == 1
        assert router.takeover_gap_s is not None
        assert router.takeover_gap_s < 1.0, "MTTR under a second, the warm-standby condition"
        r = await sclient.post("/v1/completions", json={"model": "stub-model", "prompt": "x"})
        assert r.status_code != 503, "the door is open; any failure now is the stub fleet's"
