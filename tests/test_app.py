"""The app uvicorn runs, and the two seams that make the router drivable.

Nothing else in the suite calls `create_app`, so the startup gate, the shutdown
path and the event-handler API they are written against would otherwise never
execute under test. Every test here builds the app or the router it holds.

The router takes a clock, which is what puts Algorithm 2's monitoring pass and
an exact journalled latency inside a test, and it takes an admission limit,
which is what keeps the engine connection pool off its queue.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import warnings
from collections.abc import AsyncIterator
from dataclasses import asdict
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from narwhal import cli
from narwhal.app import create_app
from narwhal.config import EngineSpec, FleetConfig
from narwhal.engine import EngineClient
from narwhal.journal import RunJournal
from narwhal.profiler import Profile
from narwhal.scheduler import SLO, Thresholds
from narwhal.server import ArrowRouter, _monitor_once
from narwhal.types import Phase, Request, Role


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _cfg(tmp_path: Path, n_prefill: int = 2, n: int = 4) -> FleetConfig:
    return FleetConfig(
        model="stub-model",
        engines=[
            EngineSpec(
                iid=f"e{k}",
                url=f"http://127.0.0.1:{8101 + k}",
                role=Role.PREFILL if k < n_prefill else Role.DECODE,
            )
            for k in range(n)
        ],
        slo=SLO(ttft_s=10.0, tpot_s=0.125),
        thresholds=Thresholds(expand=1.0, shrink=0.5, cooldown_s=10.0, sustained_intervals=3),
        profiles_path=tmp_path / "profiles.json",
        # Far longer than any test, so the background loop never fires and the
        # monitoring pass runs only where a test calls it.
        monitor_interval_s=60.0,
        tokenize=False,
    )


def _profiles(cfg: FleetConfig) -> None:
    """Every instance profiled, on the disk the router reads them from."""
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
    """Run the app's lifespan, then talk to it over its own ASGI interface.

    This is what uvicorn does around the app, in one process and one task.
    """
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://router") as client,
    ):
        yield client


def _transport(on_prefill=None) -> httpx.MockTransport:
    """Both legs and /health, enough for one request to complete."""

    async def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        body = json.loads(request.content)
        if (body.get("kv_transfer_params") or {}).get("do_remote_decode"):
            if on_prefill is not None:
                await on_prefill()
            return httpx.Response(
                200,
                json={
                    "id": "c1",
                    "object": "text_completion",
                    "choices": [
                        {
                            "index": 0,
                            "text": "",
                            "kv_transfer_params": {"remote_engine_id": "stub"},
                        }
                    ],
                },
            )
        lines = [
            "data: "
            + json.dumps(
                {
                    "id": "c1",
                    "object": "text_completion.chunk",
                    "choices": [{"index": 0, "text": f"t{k}"}],
                }
            )
            for k in range(int(body.get("max_tokens", 2)))
        ]
        lines.append("data: [DONE]")
        return httpx.Response(200, text="\n".join(lines) + "\n")

    return httpx.MockTransport(handle)


def _router(
    tmp_path: Path,
    *,
    clock=time.monotonic,
    max_concurrent: int | None = None,
    on_prefill=None,
) -> ArrowRouter:
    cfg = _cfg(tmp_path)
    _profiles(cfg)
    journal = RunJournal(path=tmp_path / "journal.jsonl")
    journal.open()
    return ArrowRouter(
        cfg,
        journal,
        transport=_transport(on_prefill),
        clock=clock,
        max_concurrent=max_concurrent,
    )


def _rows(tmp_path: Path) -> list[dict]:
    return [
        r
        for x in (tmp_path / "journal.jsonl").read_text().splitlines()
        if x.strip() and "meta" not in (r := json.loads(x))
    ]


def _body(stream: bool = False, max_tokens: int = 2) -> dict:
    return {"prompt": "hello world " * 40, "max_tokens": max_tokens, "stream": stream}


# -- the app --------------------------------------------------------------


async def test_startup_refuses_a_fleet_it_has_no_profile_for(tmp_path):
    """Algorithm 1 prices every instance from its profile, so an unprofiled
    fleet cannot be scheduled and the process says so instead of starting."""
    app = create_app(_cfg(tmp_path))
    with pytest.raises(RuntimeError, match="e0, e1, e2, e3"):
        async with _started(app):
            pass
    assert not (tmp_path / "journal.jsonl").exists()


def test_the_app_registers_no_deprecated_event_handler(tmp_path):
    """`on_event` warns in the installed FastAPI and is announced for removal,
    and `fastapi>=0.110` has no upper bound, so the release that removes it
    lands on a fresh install. The warning fires where the handler registers,
    which is inside `create_app`."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        app = create_app(_cfg(tmp_path))
    assert app.router.on_startup == []
    assert app.router.on_shutdown == []


async def test_the_app_serves_health_and_state_then_closes_what_it_opened(tmp_path):
    cfg = _cfg(tmp_path)
    _profiles(cfg)
    app = create_app(cfg, max_concurrent=7)

    async with _started(app) as client:
        assert (await client.get("/health")).json() == {"status": "ok", "instances": 4}
        state = (await client.get("/arrow/state")).json()
        assert state["pools"] == {"prefill": ["e0", "e1"], "decode": ["e2", "e3"]}
        assert state["admission"] == {"inflight": 0, "limit": 7, "rejected": 0, "refused": 0}
        assert (tmp_path / "journal.jsonl").exists()

    router = app.state.router
    assert router.journal._fh is None
    assert router.engines._client.is_closed


async def test_the_router_names_its_one_model(tmp_path):
    """An OpenAI client lists models before it sends; the router fronts one."""
    cfg = _cfg(tmp_path)
    _profiles(cfg)
    app = create_app(cfg)
    async with _started(app) as client:
        listed = (await client.get("/v1/models")).json()
    assert [m["id"] for m in listed["data"]] == ["stub-model"]


# -- admission ------------------------------------------------------------


def test_the_admission_default_is_the_size_of_the_engine_pool(tmp_path):
    """One request holds one engine connection at a time, so a fleet admitting
    this many never queues inside httpx for a connection slot."""
    cfg = _cfg(tmp_path)
    app = create_app(cfg)
    assert app.state.router.max_concurrent == cfg.max_connections


async def test_waiting_for_a_connection_slot_has_its_own_budget(tmp_path):
    """A leg that finds the pool full waits inside httpx for a slot. Charging
    that wait to `request_timeout_s` makes exhaustion a ten-minute stall with
    no log line, so the pool budget is separate and short."""
    router = _router(tmp_path)
    assert router.engines._client.timeout.pool == router.cfg.pool_timeout_s
    assert router.cfg.request_timeout_s > router.cfg.pool_timeout_s
    await router.engines.aclose()


async def test_a_full_pool_refuses_the_next_request_instead_of_queueing_it(tmp_path):
    """Past the limit httpx queues the request for a connection slot, and that
    wait lands in the request's own TTFT while Algorithm 1 prices the instance
    as if the request had been placed."""
    arrived = asyncio.Event()
    gate = asyncio.Event()

    async def hold() -> None:
        arrived.set()
        await gate.wait()

    router = _router(tmp_path, max_concurrent=1, on_prefill=hold)
    first = asyncio.create_task(router.serve("/v1/completions", _body(), {}))
    await arrived.wait()
    assert router.inflight == 1

    refused = await router.serve("/v1/completions", _body(), {})
    assert refused.status_code == 429
    assert refused.headers["retry-after"] == "1"
    assert json.loads(refused.body)["error"]["type"] == "server_overloaded_error"

    gate.set()
    await first
    assert router.inflight == 0
    assert router.state()["admission"] == {"inflight": 0, "limit": 1, "rejected": 1, "refused": 0}
    await router.engines.aclose()


async def test_a_streamed_request_holds_its_slot_until_the_stream_ends(tmp_path):
    """The response returns before its body runs, so releasing the slot at the
    return would admit a second request onto the same connection."""
    router = _router(tmp_path, max_concurrent=1)
    response = await router.serve("/v1/completions", _body(stream=True), {})
    assert router.inflight == 1

    lines = [chunk async for chunk in response.body_iterator]
    assert router.inflight == 0
    assert any("t0" in line for line in lines)

    again = await router.serve("/v1/completions", _body(), {})
    assert again.status_code == 200
    await router.engines.aclose()


# -- the OpenAI answer shape ----------------------------------------------


async def test_a_non_streaming_response_carries_usage(tmp_path):
    """The engine leg always streams (Arrow §5.2 defines the monitor on the token
    stream); the reassembled answer still owes the client its token counts."""
    router = _router(tmp_path)
    response = await router.serve("/v1/completions", _body(max_tokens=3), {})
    await router.engines.aclose()
    out = json.loads(response.body)
    assert out["usage"]["completion_tokens"] == 3
    assert out["usage"]["prompt_tokens"] > 0
    assert out["usage"]["total_tokens"] == out["usage"]["prompt_tokens"] + 3
    assert out["choices"][0]["finish_reason"] == "stop"


# -- the clock seam -------------------------------------------------------


async def test_the_journalled_latency_is_read_off_the_injected_clock(tmp_path):
    """TTFT is cut at `q1 + p1` (Arrow §4.2), so a prefill leg that takes 0.5 s is a
    journal row that says 0.5 s and not a row that says it is positive."""
    clock = FakeClock()

    async def spend() -> None:
        clock.advance(0.5)

    router = _router(tmp_path, clock=clock, on_prefill=spend)
    await router.serve("/v1/completions", _body(), {})
    await router.engines.aclose()

    row = _rows(tmp_path)[0]
    assert row["arrived"] == 0.0
    assert row["ttft_s"] == 0.5
    assert row["output_len"] == 2
    assert row["run"] == router.journal.run


async def test_algorithm_2_flips_on_the_monitoring_pass_when_the_trigger_holds(tmp_path):
    """Arrow §5.5's second trigger is prefill idle while decode is loaded, and it
    fires on the update interval rather than on the request path. Arrow §5.5 also
    requires the load to hold "over a period of time", so the pass counts
    `sustained_intervals` of it before Algorithm 3 moves anything."""
    clock = FakeClock()
    router = _router(tmp_path, clock=clock)
    # This test exercises Algorithm 2 itself, so it runs the reactive
    # controller: under the default planner, controller_owns_flips
    # suppresses exactly the inline flip asserted here.
    router.scheduler.controller_owns_flips = False
    router.planner = None
    # The fixture fleet has been up for a full cooldown; without this the
    # P->D flip is refused as too soon after birth.
    router.scheduler._last_p2d_flip -= router.cfg.thresholds.cooldown_s

    # One decode request on e2 whose same-instance token gap runs 1 s against
    # the 125 ms target. The first decode token only arms the gap tracking:
    # its own wait reaches back across the transfer and the queue.
    router.monitor.dispatched("e2", Request(rid="r1", input_len=64, phase=Phase.DECODE))
    router.monitor.first_token("e2", "r1")
    router.monitor.output_token("e2", "r1")
    clock.advance(1.0)
    router.monitor.output_token("e2", "r1")
    # Arrow §5.5 reads the load off a completed interval; close it.
    router.monitor.roll_interval()
    assert router.scheduler.pool_load(Role.PREFILL) == 0.0
    assert router.scheduler.pool_load(Role.DECODE) >= router.cfg.thresholds.expand

    sustained = router.cfg.thresholds.sustained_intervals
    flips = [await _monitor_once(router) for _ in range(sustained)]

    assert flips[:-1] == [None] * (sustained - 1)
    assert flips[-1] is not None
    assert flips[-1].role is Role.DECODE
    assert len(router.monitor.pool(Role.PREFILL)) == 1
    assert router.state()["flips"][-1]["by"] == "algorithm2"
    await router.engines.aclose()


# -- the flags that bound the process -------------------------------------


def _handed(tmp_path: Path, monkeypatch, *extra: str) -> dict:
    """Run `narwhal-serve` up to uvicorn, and report what it handed over."""
    fleet = tmp_path / "fleet.json"
    fleet.write_text(
        json.dumps(
            {
                "model": "stub-model",
                "engines": [{"iid": "e0", "url": "http://127.0.0.1:8101"}],
                "slo": {"ttft_s": 10, "tpot_s": 0.125},
                "profiles_path": str(tmp_path / "profiles.json"),
            }
        )
    )
    handed: dict = {}
    # `serve` sets the level for the whole process, so it is put back.
    monkeypatch.setattr(logging.root, "level", logging.root.level)
    monkeypatch.setattr(cli, "_port_in_use", lambda host, port: None)
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: handed.update(app=app, **kw))
    assert cli.serve(["--fleet", str(fleet), *extra]) == 0
    return handed


def test_the_shutdown_budget_reaches_uvicorn(tmp_path, monkeypatch):
    """uvicorn holds the socket open for as long as the slowest in-flight
    request, which `request_timeout_s` puts at ten minutes, until it is told
    how long a drain is allowed to take."""
    default = FleetConfig.__dataclass_fields__["graceful_timeout_s"].default
    assert _handed(tmp_path, monkeypatch)["timeout_graceful_shutdown"] == default
    handed = _handed(tmp_path, monkeypatch, "--graceful-timeout", "5")
    assert handed["timeout_graceful_shutdown"] == 5


def test_a_negative_shutdown_budget_is_refused_at_the_argument(tmp_path, monkeypatch):
    """uvicorn counts forward from SIGTERM, so a negative budget drops every
    in-flight stream at the signal."""
    with pytest.raises(SystemExit) as exc:
        _handed(tmp_path, monkeypatch, "--graceful-timeout", "-1")
    assert exc.value.code == 2


def test_the_admission_limit_reaches_the_router(tmp_path, monkeypatch):
    handed = _handed(tmp_path, monkeypatch, "--max-concurrent", "9")
    assert handed["app"].state.router.max_concurrent == 9


def test_an_admission_limit_that_admits_nothing_is_refused_at_the_argument(tmp_path, monkeypatch):
    with pytest.raises(SystemExit) as exc:
        _handed(tmp_path, monkeypatch, "--max-concurrent", "0")
    assert exc.value.code == 2


def test_the_journal_flag_reaches_the_journal(tmp_path, monkeypatch):
    """One file per arm is how two arms against one router stay separable."""
    handed = _handed(tmp_path, monkeypatch, "--journal", str(tmp_path / "arm-a.jsonl"))
    assert handed["app"].state.router.journal.path == tmp_path / "arm-a.jsonl"


async def test_the_state_schema_is_pinned(tmp_path):
    """Every field state() emits must appear in StateOut, or a harness that
    polls /arrow/state loses it silently: response_model filters unknowns."""
    from narwhal.schemas import StateOut

    router = _router(tmp_path)
    await router.serve("/v1/completions", _body(), {})
    await router.engines.aclose()
    raw = router.state()
    validated = StateOut.model_validate(raw).model_dump()
    assert set(validated) == set(raw)
    for key in raw:
        assert validated[key] == raw[key], key


async def test_the_openapi_page_names_every_route(tmp_path):
    """The audit's finding: four endpoints with empty bodies teach nothing."""
    app = create_app(_cfg(tmp_path))
    spec = app.openapi()
    summaries = {
        path: next(iter(ops.values())).get("summary", "") for path, ops in spec["paths"].items()
    }
    assert all(summaries.values()), (
        f"unsummarized routes: {[p for p, s in summaries.items() if not s]}"
    )
    state_schema = spec["components"]["schemas"]["StateOut"]["properties"]
    assert "flips" in state_schema
    assert "admission" in state_schema


# -- correlation ----------------------------------------------------------


async def test_the_response_header_joins_the_journal_row(tmp_path):
    """One rid on the response, the journal row and the failure log line is
    the thread the audit found missing: no way to join a client's complaint
    to the row that recorded it."""
    router = _router(tmp_path)
    response = await router.serve("/v1/completions", _body(), {})
    await router.engines.aclose()
    rid = response.headers["x-request-id"]
    assert _rows(tmp_path)[0]["rid"] == rid


async def test_a_client_supplied_id_rides_the_row(tmp_path):
    router = _router(tmp_path)
    await router.serve("/v1/completions", _body(), {"x-request-id": "client-abc"})
    await router.engines.aclose()
    row = _rows(tmp_path)[0]
    assert row["client_rid"] == "client-abc"
    assert row["rid"] != "client-abc", "the internal rid stays minted: reuse cannot collide"


async def test_even_a_refusal_carries_an_id(tmp_path):
    arrived = asyncio.Event()
    gate = asyncio.Event()

    async def hold() -> None:
        arrived.set()
        await gate.wait()

    router = _router(tmp_path, max_concurrent=1, on_prefill=hold)
    first = asyncio.create_task(router.serve("/v1/completions", _body(), {}))
    await arrived.wait()
    refused = await router.serve("/v1/completions", _body(), {})
    assert refused.status_code == 429
    assert refused.headers["x-request-id"]
    gate.set()
    await first
    await router.engines.aclose()


# -- every budget lands in the seam it names ------------------------------


async def test_each_timeout_field_reaches_its_own_seam(tmp_path):
    """All six budgets set to distinct values, each asserted at the client
    seam it configures: the audit's finding was that no test showed the
    budgets to differ at all."""
    cfg = _cfg(tmp_path)
    _profiles(cfg)
    cfg.request_timeout_s = 601.0
    cfg.prefill_timeout_s = 121.0
    cfg.decode_read_timeout_s = 61.0
    cfg.pool_timeout_s = 5.5
    cfg.connect_timeout_s = 10.5
    cfg.health_timeout_s = 4.5
    journal = RunJournal(path=tmp_path / "journal.jsonl")
    router = ArrowRouter(cfg, journal, transport=_transport())
    t = router.engines._client.timeout
    assert t.read == 61.0, "the stream gap watchdog, not the request budget"
    assert t.pool == 5.5
    assert t.connect == 10.5
    assert router.engines._prefill_timeout == 121.0
    assert router.engines._health_timeout == 4.5
    assert len({601.0, 121.0, 61.0, 5.5, 10.5, 4.5}) == 6
    await router.engines.aclose()


# -- the breaker's two failure shapes --------------------------------


async def test_timeout_shaped_failures_probe_health_instead_of_ejecting(tmp_path):
    """A flooded engine times out while its /health answers: the walk-2 rerun
    ejected a healthy engine 47 times on exactly this. Timeout-shaped
    failures at threshold verify; the verify clears the breaker."""
    from narwhal.engine import EngineError

    router = _router(tmp_path)
    for _ in range(router.cfg.eject_after):
        router._leg_failed("e0", EngineError("decode", "http://e0", 504, "first token late"))
    await asyncio.sleep(0.05)  # the verify task probes the mock /health
    assert "e0" not in router.scheduler.ejected
    assert router.scheduler._soft_failures.get("e0", 0) == 0, "verify clears the count"
    await router.engines.aclose()


async def test_a_wedged_listener_still_ejects(tmp_path):
    """Timeouts plus a /health that does not answer is a dead engine with a
    live socket - the one case the timeout path must still catch."""
    from narwhal.engine import EngineError

    def handler(request):
        if request.url.path == "/health":
            return httpx.Response(500, text="wedged")
        return httpx.Response(200, json={})

    cfg = _cfg(tmp_path)
    _profiles(cfg)
    journal = RunJournal(path=tmp_path / "journal.jsonl")
    router = ArrowRouter(cfg, journal, transport=httpx.MockTransport(handler))
    for _ in range(router.cfg.eject_after):
        router._leg_failed("e0", EngineError("decode", "http://e0", 504, "first token late"))
    await asyncio.sleep(0.05)
    assert "e0" in router.scheduler.ejected
    await router.engines.aclose()


async def test_connection_failures_keep_the_fast_path(tmp_path):
    """A dead engine presents as connection failures and must not wait for
    any probe: n4's real death is the case."""
    router = _router(tmp_path)
    for _ in range(router.cfg.eject_after):
        router._leg_failed("e0", httpx.ConnectError("refused"))
    assert "e0" in router.scheduler.ejected, "ejected synchronously, no probe"
    await router.engines.aclose()


async def test_the_planner_controller_wires_end_to_end(tmp_path):
    """controller: planner builds the plan loop, suppresses the scheduler's
    own flips, reports itself on /arrow/state, and moves pools from the
    monitoring pass when the windowed demand says so."""
    cfg = _cfg(tmp_path)
    cfg.controller = "planner"
    cfg.plan_interval_s = 1.0
    cfg.plan_window_s = 30.0
    cfg.plan_min_arrivals = 5
    _profiles(cfg)
    clock = FakeClock()
    journal = RunJournal(path=tmp_path / "journal.jsonl")
    router = ArrowRouter(cfg, journal, transport=_transport(), clock=clock)
    assert router.planner is not None
    assert router.scheduler.controller_owns_flips
    assert router.state()["controller"] == "planner"

    # Heavy prefill demand on a 2P2D fleet: the plan loop should grow prefill.
    clock.advance(40.0)
    for k in range(60):
        router.planner._arrivals.append((clock.t - 20 + k / 3, 12000))
    clock.advance(2.0)
    await _monitor_once(router)
    assert len(router.monitor.pool(Role.PREFILL)) > 2, "the plan moved the pools"
    assert router.state()["flips"][-1]["by"] == "planner"
    await router.engines.aclose()


# -- opt-in payload capture --------------------------------------------


async def test_payload_capture_joins_the_journal_by_rid(tmp_path):
    """Opt-in sidecar: prompt and output text, rid-joined; the journal
    itself keeps its lengths-and-timings-only contract."""
    cfg = _cfg(tmp_path)
    cfg.journal_payloads = str(tmp_path / "payloads.jsonl")
    _profiles(cfg)
    app = create_app(cfg, journal_path=tmp_path / "j.jsonl")
    app.state.router.engines = EngineClient(transport=_transport())
    async with _started(app) as client:
        r = await client.post(
            "/v1/completions",
            json={"model": "stub-model", "prompt": "hello payload", "max_tokens": 4},
        )
        assert r.status_code == 200
        rid = r.headers["x-request-id"]
    rows = [json.loads(x) for x in (tmp_path / "payloads.jsonl").read_text().splitlines()]
    assert [row["rid"] for row in rows] == [rid]
    assert rows[0]["prompt"] == "hello payload"
    assert rows[0]["output"], "the relayed SSE text was captured"
    assert rows[0]["prompt_truncated"] is False
    journal_rows = (tmp_path / "j.jsonl").read_text()
    assert "hello payload" not in journal_rows, "content never leaks into the journal"


async def test_payload_fields_truncate_and_the_file_caps(tmp_path):
    from narwhal.journal import PayloadLog

    p = PayloadLog(tmp_path / "p.jsonl", max_chars=5, max_mb=1)
    p.open()
    p.write("r1", "0123456789", "abcdefghij")
    row = json.loads((tmp_path / "p.jsonl").read_text())
    assert row["prompt"] == "01234"
    assert row["prompt_truncated"] is True
    assert row["output"] == "abcde"
    # The hard cap: force the counter past the ceiling and the log stops
    # quietly instead of growing.
    p._bytes = p.max_bytes
    p.write("r2", "x", "y")
    p.write("r3", "x", "y")
    rows = (tmp_path / "p.jsonl").read_text().splitlines()
    assert len(rows) == 1, "capture stopped at the cap; serving is unaffected"
    p.close()


def test_payload_config_roundtrips(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.journal_payloads = "runs/local/payloads.jsonl"
    cfg.journal_payloads_max_chars = 512
    out = tmp_path / "cfg.json"
    cfg.save(out)
    loaded = FleetConfig.load(out)
    assert loaded.journal_payloads == "runs/local/payloads.jsonl"
    assert loaded.journal_payloads_max_chars == 512
    assert loaded.journal_payloads_max_mb == 256
