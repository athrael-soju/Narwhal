"""The serving path, against a stub that speaks vLLM's disaggregated protocol.

These tests exist because the split is the part that has actually failed in
production. A scheduler that prices instances perfectly is worth nothing if the
prefill leg is refused, or if the handoff parameters never reach the decode
leg, or if a failed request is never retired from its instance. Each of those
is a test here.

The stub records what it was sent, so the assertions are about the wire, not
about the router's own opinion of what it did.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from narwhal.config import EngineSpec, FleetConfig
from narwhal.engine import EngineClient, EngineError, sse_token_count
from narwhal.journal import RunJournal
from narwhal.profiler import Profile
from narwhal.scheduler import SLO, Thresholds
from narwhal.server import (
    ArrowRouter,
    _monitor_once,
    _readmit,
    _reassemble,
    _sweep_liveness,
)
from narwhal.types import Phase, Request, Role

_EJECT_AFTER = FleetConfig.__dataclass_fields__["eject_after"].default


class StubFleet:
    """Every engine in one transport, keyed by the port in the URL."""

    def __init__(
        self,
        *,
        refuse_prefill: bool = False,
        no_kv_params: bool = False,
        dead_decode: set[str] | None = None,
        dead_after_tokens: int = 0,
        refuse_decode: int = 0,
        unreachable: set[str] | None = None,
    ) -> None:
        self.prefill_bodies: list[dict] = []
        self.decode_bodies: list[dict] = []
        self.decode_targets: list[str] = []
        self.refuse_prefill = refuse_prefill
        self.no_kv_params = no_kv_params
        # Instance ids whose decode leg times out, the shape a wedged
        # KV pairing produces. `dead_after_tokens` streams that many first.
        self.dead_decode = dead_decode or set()
        self.dead_after_tokens = dead_after_tokens
        self.refuse_decode = refuse_decode
        # Instance ids refusing every connection, as a dead engine does.
        self.unreachable = unreachable or set()
        self.tokenize_targets: list[str] = []
        self.tokenize_budgets: list[float | None] = []
        # Called with the instance id serving a prefill leg, so a test can make
        # traffic arrive while that leg is in flight.
        self.on_prefill = None

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        iid = f"e{request.url.port - 8101}"
        if iid in self.unreachable:
            raise httpx.ConnectError("connection refused", request=request)
        path = request.url.path
        if path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/tokenize":
            self.tokenize_targets.append(iid)
            self.tokenize_budgets.append((request.extensions.get("timeout") or {}).get("read"))
            body = json.loads(request.content)
            return httpx.Response(200, json={"count": len(str(body.get("prompt", ""))) // 4})

        body = json.loads(request.content)
        is_prefill = (body.get("kv_transfer_params") or {}).get("do_remote_decode") is True
        if is_prefill:
            self.prefill_bodies.append(body)
            if self.on_prefill is not None:
                self.on_prefill(f"e{request.url.port - 8101}")
            if self.refuse_prefill:
                return httpx.Response(400, text="min_tokens must be less than max_tokens=1")
            params = (
                {} if self.no_kv_params else {"remote_engine_id": "stub", "remote_block_ids": [1]}
            )
            return httpx.Response(
                200,
                json={
                    "id": "c1",
                    "object": "text_completion",
                    "choices": [{"index": 0, "text": "", "kv_transfer_params": params}],
                },
            )

        self.decode_bodies.append(body)
        self.decode_targets.append(iid)
        if self.refuse_decode:
            return httpx.Response(self.refuse_decode, text="engine refused the leg")
        if iid in self.dead_decode:
            if not self.dead_after_tokens:
                raise httpx.ReadTimeout("", request=request)
            partial = [
                "data: "
                + json.dumps(
                    {
                        "id": "c1",
                        "object": "text_completion.chunk",
                        "choices": [{"index": 0, "text": f"t{k}"}],
                    }
                )
                for k in range(self.dead_after_tokens)
            ]
            return httpx.Response(200, text="\n".join(partial) + "\n")
        n = int(body.get("max_tokens", 3))
        lines = [
            "data: "
            + json.dumps(
                {
                    "id": "c1",
                    "object": "text_completion.chunk",
                    "choices": [{"index": 0, "text": f"t{k}"}],
                }
            )
            for k in range(n)
        ]
        lines.append("data: [DONE]")
        return httpx.Response(200, text="\n".join(lines) + "\n")


def _journal_rows(tmp_path) -> list[dict]:
    """Request rows only; the provenance meta row is the journal's, not a request."""
    return [
        r
        for x in (tmp_path / "journal.jsonl").read_text().splitlines()
        if x.strip() and "meta" not in (r := json.loads(x))
    ]


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
        thresholds=Thresholds(expand=1.0, shrink=0.5, cooldown_s=10.0),
        profiles_path=tmp_path / "profiles.json",
    )


def _router(tmp_path: Path, stub: StubFleet, **kw) -> ArrowRouter:
    cfg = _cfg(tmp_path, **kw)
    journal = RunJournal(path=tmp_path / "journal.jsonl")
    journal.open()
    r = ArrowRouter(cfg, journal, transport=stub.transport())
    for spec in cfg.engines:
        r.profiles.put(
            Profile(
                iid=spec.iid,
                ttft_a=2e-8,
                ttft_b=6e-5,
                ttft_c=0.005,
                tpot_slope=3e-6,
                tpot_intercept=0.012,
            )
        )
    return r


def _body(prompt: str = "hello world " * 40, max_tokens: int = 4) -> dict:
    return {"prompt": prompt, "max_tokens": max_tokens, "stream": False}


# -- the wire -------------------------------------------------------------


async def test_prefill_leg_forces_one_token_and_asks_for_remote_decode(tmp_path):
    """The prefill leg is the client body with three fields overridden."""
    stub = StubFleet()
    r = _router(tmp_path, stub)
    await r.serve("/v1/completions", _body(), {})
    assert len(stub.prefill_bodies) == 1
    leg = stub.prefill_bodies[0]
    assert leg["max_tokens"] == 1
    assert leg["stream"] is False
    assert leg["kv_transfer_params"] == {"do_remote_decode": True}
    await r.engines.aclose()


async def test_prefill_leg_drops_the_fields_its_overrides_contradict(tmp_path):
    """`stream_options` and `min_tokens` are 400s on a one-token prefill leg.

    This combination is what `vllm bench serve` sends by default, and it failed
    every split request on a real fleet for hours while the un-split path
    served normally.
    """
    stub = StubFleet()
    r = _router(tmp_path, stub)
    body = _body()
    body["stream_options"] = {"include_usage": True}
    body["min_tokens"] = 4
    await r.serve("/v1/completions", body, {})
    leg = stub.prefill_bodies[0]
    assert "stream_options" not in leg
    assert "min_tokens" not in leg
    await r.engines.aclose()


async def test_a_disaggregated_fleet_crosses_the_two_phases(tmp_path):
    """Arrow §4.1 assumes "prefill instances process requests sequentially, while
    decode instances maximize batch size", which is what the profile measures,
    so each phase is scheduled over the pool that does that work and the KV
    crosses."""
    stub = StubFleet()
    r = _router(tmp_path, stub)
    await r.serve("/v1/completions", _body(), {})
    row = _journal_rows(tmp_path)[0]
    assert row["prefill_iid"] != row["decode_iid"]
    assert r.monitor.instances[row["prefill_iid"]].role is Role.PREFILL
    assert r.monitor.instances[row["decode_iid"]].role is Role.DECODE
    assert row["crossed"] is True
    await r.engines.aclose()


async def test_an_all_decode_fleet_keeps_both_phases_on_one_instance(tmp_path):
    """Arrow §5.2: Arrow "may even assign both phases to the same instance if
    desired". Labelling every engine decode is how the aggregated arm asks."""
    stub = StubFleet()
    r = _router(tmp_path, stub, n_prefill=0)
    await r.serve("/v1/completions", _body(), {})
    row = _journal_rows(tmp_path)[0]
    assert row["prefill_iid"] == row["decode_iid"]
    assert row["crossed"] is False
    assert "kv_transfer_params" not in stub.decode_bodies[0]
    await r.engines.aclose()


async def test_handoff_parameters_reach_the_decode_leg_when_it_crosses(tmp_path):
    """A busy prefill instance pushes decode elsewhere, and the KV follows.

    The busy state is made by a second request arriving on that instance while
    the first one's prefill leg is in flight, which is the ordinary concurrent
    case rather than a contrivance: §5.3's decode cost sorts on resident prefill
    work, so the instance still prefilling is the one decode avoids.
    """
    stub = StubFleet()
    r = _router(tmp_path, stub)

    def crowd(iid: str) -> None:
        from narwhal.types import Phase, Request

        r.monitor.dispatched(iid, Request(rid="other", input_len=8000, phase=Phase.PREFILL))

    stub.on_prefill = crowd
    await r.serve("/v1/completions", _body(), {})

    row = _journal_rows(tmp_path)[0]
    assert row["prefill_iid"] != row["decode_iid"]
    assert row["crossed"] is True
    leg = stub.decode_bodies[0]
    assert leg["kv_transfer_params"] == {"remote_engine_id": "stub", "remote_block_ids": [1]}
    assert leg["stream"] is True
    await r.engines.aclose()


async def test_no_handoff_when_both_phases_land_on_one_instance(tmp_path):
    """Arrow §5.2 allows both phases on one instance, and then there is no transfer.

    A one-instance fleet forces the case. The KV is already in that engine's
    own prefix cache, so sending handoff parameters would ask it to fetch from
    itself.
    """
    stub = StubFleet()
    r = _router(tmp_path, stub, n_prefill=1, n=1)
    await r.serve("/v1/completions", _body(), {})
    assert "kv_transfer_params" not in stub.decode_bodies[0]
    row = _journal_rows(tmp_path)[0]
    assert row["crossed"] is False
    assert row["prefill_iid"] == row["decode_iid"]
    await r.engines.aclose()


async def test_the_engine_is_always_streamed_even_for_a_whole_body_client(tmp_path):
    """Arrow §5.2 builds the monitor on the token stream, so the leg always streams.

    A non-streamed decode leg would leave decode load reading zero forever, and
    Algorithm 2's expand trigger would never fire.
    """
    stub = StubFleet()
    r = _router(tmp_path, stub)
    resp = await r.serve("/v1/completions", _body(max_tokens=5), {})
    assert stub.decode_bodies[0]["stream"] is True
    assert resp.status_code == 200
    payload = json.loads(bytes(resp.body))
    assert payload["choices"][0]["text"] == "t0t1t2t3t4"
    await r.engines.aclose()


# -- accounting -----------------------------------------------------------


async def test_a_completed_request_leaves_no_residency_behind(tmp_path):
    """Every instance is empty again once the request is done.

    A leaked sub-request inflates that instance's cost for the rest of the run,
    so every later Algorithm 1 decision routes around a load that is not there.
    """
    stub = StubFleet()
    r = _router(tmp_path, stub)
    await r.serve("/v1/completions", _body(), {})
    for inst in r.monitor.instances.values():
        assert not inst.prefill
        assert not inst.decode
    await r.engines.aclose()


async def test_a_refused_prefill_leg_is_retired_and_journalled(tmp_path):
    """A 400 on the prefill leg must not strand the request on the instance."""
    stub = StubFleet(refuse_prefill=True)
    r = _router(tmp_path, stub)
    resp = await r.serve("/v1/completions", _body(), {})
    assert resp.status_code == 502
    assert r.failed == 1
    for inst in r.monitor.instances.values():
        assert not inst.prefill
        assert not inst.decode
    row = _journal_rows(tmp_path)[0]
    assert row["error"]
    assert row["ttft_s"] is None
    await r.engines.aclose()


async def test_a_build_without_nixl_is_named_rather_than_guessed_at(tmp_path):
    """An empty handoff means the connector is absent, and the error says so."""
    stub = StubFleet(no_kv_params=True)
    r = _router(tmp_path, stub)
    resp = await r.serve("/v1/completions", _body(), {})
    assert resp.status_code == 502
    detail = json.loads(bytes(resp.body))["error"]["message"]
    assert "NixlConnector" in detail
    await r.engines.aclose()


async def test_the_journal_records_both_instances_and_the_measured_latencies(tmp_path):
    stub = StubFleet()
    r = _router(tmp_path, stub)
    await r.serve("/v1/completions", _body(max_tokens=6), {})
    row = _journal_rows(tmp_path)[0]
    assert row["output_len"] == 6
    assert row["ttft_s"] > 0
    assert row["tpot_s"] is not None
    assert row["prefill_iid"] in r.monitor.instances
    assert row["decode_iid"] in r.monitor.instances
    assert row["error"] is None
    await r.engines.aclose()


async def test_the_monitor_sees_every_output_token(tmp_path):
    """The decode load term is built from these events and nothing else.

    Arrow §5.5 reads it off a completed interval, so the loop has to close one before
    the value is published."""
    stub = StubFleet()
    r = _router(tmp_path, stub)
    await r.serve("/v1/completions", _body(max_tokens=8), {})
    r.monitor.roll_interval()
    assert any(r.monitor.mean_token_interval(iid) > 0 for iid in r.monitor.instances)
    await r.engines.aclose()


# -- units ----------------------------------------------------------------


async def test_the_door_prices_prefix_identity_only_when_an_arm_is_on(tmp_path):
    """The door hashes the prompt head and prices the reuse term from it:
    with coop on, one serve leaves a warm record keyed by that hash; with
    both arms off, nothing is recorded at all."""
    stub = StubFleet()
    router = _router(tmp_path, stub)
    await router._serve("r0", "/v1/completions", _body(), {})
    assert router.scheduler._warm == {}, "no arm configured: no identity priced"

    coop_cfg = _cfg(tmp_path)
    coop_cfg.prefix_coop = True
    journal = RunJournal(path=tmp_path / "journal-coop.jsonl")
    journal.open()
    warmed = ArrowRouter(coop_cfg, journal, transport=stub.transport())
    for spec in coop_cfg.engines:
        warmed.profiles.put(router.profiles.get(spec.iid))
    await warmed._serve("r1", "/v1/completions", _body(), {})

    (key, home), (tokens, _touched) = next(iter(warmed.scheduler._warm.items()))
    prompt_tokens = len(_body()["prompt"]) // 4
    assert isinstance(key, int)
    assert home in {"e0", "e1", "e2", "e3"}
    assert tokens == prompt_tokens


def test_sse_token_count_ignores_the_terminator_and_counts_deltas():
    assert sse_token_count('data: {"choices":[{"text":"a"}]}') == 1
    assert sse_token_count('data: {"choices":[{"delta":{"content":"a"}}]}') == 1
    assert sse_token_count("data: [DONE]") == 0
    assert sse_token_count(": keep-alive") == 0
    assert sse_token_count('data: {"choices":[]}') == 0


def test_reassemble_folds_a_streamed_leg_into_one_body():
    lines = [
        'data: {"id":"c","object":"text_completion.chunk","choices":[{"text":"a"}]}',
        'data: {"id":"c","object":"text_completion.chunk","choices":[{"text":"b"}]}',
        "data: [DONE]",
    ]
    out = _reassemble(lines)
    assert out["choices"][0]["text"] == "ab"
    assert out["object"] == "text_completion"


def test_config_rejects_a_fleet_with_no_engines(tmp_path):
    path = tmp_path / "fleet.json"
    path.write_text(json.dumps({"model": "m", "engines": [], "slo": {"ttft_s": 1, "tpot_s": 1}}))
    with pytest.raises(ValueError, match="declares no engines"):
        FleetConfig.load(path)


def test_config_reads_the_thresholds_and_the_starting_labels(tmp_path):
    path = tmp_path / "fleet.json"
    path.write_text(
        json.dumps(
            {
                "model": "m",
                "engines": [
                    {"iid": "a", "url": "http://h:1/", "role": "prefill"},
                    {"iid": "b", "url": "http://h:2"},
                ],
                "slo": {"ttft_s": 10, "tpot_s": 0.125},
                "thresholds": {"expand": 1.2, "shrink": 0.4, "cooldown_s": 30},
            }
        )
    )
    cfg = FleetConfig.load(path)
    assert cfg.engines[0].role is Role.PREFILL
    assert cfg.engines[1].role is Role.DECODE
    assert cfg.engines[0].url == "http://h:1"
    assert cfg.thresholds.expand == 1.2
    assert cfg.thresholds.cooldown_s == 30


async def test_an_unhealthy_engine_reads_unhealthy():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    c = EngineClient(transport=httpx.MockTransport(handler))
    assert await c.healthy("http://127.0.0.1:1") is False
    await c.aclose()


async def test_a_non_200_prefill_leg_raises_with_the_leg_named():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    c = EngineClient(transport=httpx.MockTransport(handler))
    with pytest.raises(EngineError) as exc:
        await c.prefill("http://127.0.0.1:1", "/v1/completions", {"prompt": "x"}, {})
    assert exc.value.leg == "prefill"
    assert exc.value.status == 500
    await c.aclose()


# -- surviving a dead KV pairing (issue #1) -------------------------------


@pytest.mark.asyncio
async def test_a_decode_leg_that_streams_nothing_is_retried_elsewhere(tmp_path):
    """A pairing can be dead while every other pairing works. Nothing has
    reached the client yet, so the request moves rather than failing."""
    stub = StubFleet()
    r = _router(tmp_path, stub)
    first = r.scheduler.schedule(Request(rid="probe", input_len=120, phase=Phase.DECODE))
    stub.dead_decode = {first.iid}

    resp = await r.serve("/v1/completions", _body(), {})

    assert resp.status_code == 200
    assert stub.decode_targets[0] == first.iid
    assert stub.decode_targets[1] != first.iid, "the retry must not reuse the dead instance"
    row = _journal_rows(tmp_path)[-1]
    assert row["error"] is None
    assert row["output_len"] == 4
    assert row["attempts"] == stub.decode_targets[:2]
    assert r.failed == 0


@pytest.mark.asyncio
async def test_a_failure_after_the_first_token_is_not_retried(tmp_path):
    """Once a token is streamed the response is committed, so a retry would
    duplicate output the client already holds."""
    stub = StubFleet(dead_after_tokens=2)
    r = _router(tmp_path, stub)
    first = r.scheduler.schedule(Request(rid="probe", input_len=120, phase=Phase.DECODE))
    stub.dead_decode = {first.iid}

    await r.serve("/v1/completions", _body(max_tokens=6), {})

    assert stub.decode_targets == [first.iid], "no second attempt once tokens are out"
    row = _journal_rows(tmp_path)[-1]
    assert row["output_len"] == 2
    assert row["wanted_len"] == 6


@pytest.mark.asyncio
async def test_a_retry_leaves_no_residency_on_the_dead_instance(tmp_path):
    stub = StubFleet()
    r = _router(tmp_path, stub)
    first = r.scheduler.schedule(Request(rid="probe", input_len=120, phase=Phase.DECODE))
    stub.dead_decode = {first.iid}

    await r.serve("/v1/completions", _body(), {})

    for inst in r.monitor.instances.values():
        assert not inst.decode, f"{inst.iid} still holds decode work"
        assert not inst.prefill, f"{inst.iid} still holds prefill work"


@pytest.mark.asyncio
async def test_every_decode_instance_dead_fails_the_request_once(tmp_path):
    stub = StubFleet()
    r = _router(tmp_path, stub)
    stub.dead_decode = {spec.iid for spec in r.cfg.engines}

    await r.serve("/v1/completions", _body(), {})

    row = _journal_rows(tmp_path)[-1]
    assert row["error"].startswith("ReadTimeout")
    assert len(row["attempts"]) == r.cfg.decode_attempts
    assert r.failed == 1


@pytest.mark.asyncio
async def test_the_decode_read_deadline_is_shorter_than_the_request_budget(tmp_path):
    """A stalled stream must fail on the read gap, not on the whole-request
    budget, or the retry cannot fire until the request has already missed."""
    cfg = _cfg(tmp_path)
    assert cfg.decode_read_timeout_s < cfg.request_timeout_s
    r = ArrowRouter(cfg, RunJournal(path=tmp_path / "j.jsonl"), transport=StubFleet().transport())
    t = r.engines._client.timeout
    assert t.read == cfg.decode_read_timeout_s
    assert t.write == cfg.request_timeout_s


@pytest.mark.asyncio
async def test_the_first_token_deadline_sits_between_its_two_bounds(tmp_path):
    """Bounded below by the measured healthy t2 and above by the TPOT budget of
    the shortest output, because a retried request spends it all inside TPOT."""
    cfg = _cfg(tmp_path)
    worst_healthy_t2 = 0.283  # measured over 516 requests on the fleet
    shortest_output = 30  # the short end of the reference trace
    healthy_tpot = 0.026  # measured p50 on clean requests
    upper = (cfg.slo.tpot_s - healthy_tpot) * (shortest_output - 1)

    assert cfg.first_token_timeout_s > worst_healthy_t2, "would abort healthy starts"
    assert cfg.first_token_timeout_s < upper, "a retried short request would miss TPOT"
    assert cfg.first_token_timeout_s < cfg.decode_read_timeout_s


async def test_a_dead_decode_leg_is_an_error_status_not_an_empty_completion(tmp_path):
    """A failed decode leg used to return 200 with an empty body and
    finish_reason "stop", which an eval harness scores as success. A timeout
    is a 504 and the counters say failed, not served."""
    stub = StubFleet(dead_decode={"e2", "e3"})
    r = _router(tmp_path, stub)

    resp = await r.serve("/v1/completions", {"prompt": "hello", "max_tokens": 3}, {})
    assert resp.status_code == 504
    assert json.loads(resp.body)["error"]["type"] == "decode"
    assert (r.served, r.failed) == (0, 1)


async def test_an_upstream_refusal_maps_to_502(tmp_path):
    """`EngineError.status` finally gets read: an engine-side 500 is a bad
    gateway, not a timeout, so a client's retry policy sees the truth."""
    stub = StubFleet(refuse_decode=500)
    r = _router(tmp_path, stub)

    resp = await r.serve("/v1/completions", {"prompt": "hello", "max_tokens": 3}, {})
    assert resp.status_code == 502
    assert (r.served, r.failed) == (0, 1)


async def test_a_streaming_failure_ends_with_an_error_frame(tmp_path):
    """The stream is committed at 200 before the leg fails, so the failure
    must travel in-band: the terminal event is an error frame, not a
    clean-looking end of output."""
    stub = StubFleet(dead_decode={"e2", "e3"})
    r = _router(tmp_path, stub)

    resp = await r.serve(
        "/v1/completions", {"prompt": "hello", "max_tokens": 3, "stream": True}, {}
    )
    frames = [line async for line in resp.body_iterator]
    assert frames, "the failure must produce a frame"
    last = json.loads(frames[-1].removeprefix("data: "))
    assert "error" in last
    assert last["error"]["type"] == "decode"


async def test_the_wrong_model_is_refused_before_any_leg_runs(tmp_path):
    """Serving whatever name arrives attributes the numbers to a model that
    was never loaded. A mismatch is a 404 and the engines never hear of it."""
    stub = StubFleet()
    r = _router(tmp_path, stub)

    resp = await r.serve("/v1/completions", {"model": "some-other-model", "prompt": "hi"}, {})
    assert resp.status_code == 404
    assert json.loads(resp.body)["error"]["code"] == "model_not_found"
    assert stub.prefill_bodies == [], "nothing was dispatched"


# -- the breaker ----------------------------------------------


def _small_body() -> dict:
    return {"prompt": "hello", "max_tokens": 3}


async def test_a_dead_engine_leaves_the_candidate_set_rather_than_taking_the_fleet(tmp_path):
    """A refused connection accrues no residency, so §5.3 prices the instance
    at the fleet minimum and it wins every argmin. Measured twice: on a
    six-engine stub with one killed, 85% of dispatches went to the corpse;
    on the fleet, a dead engine absorbed 2,748 requests over 40 minutes."""
    stub = StubFleet(unreachable={"e0"})
    r = _router(tmp_path, stub, n=6)

    for _ in range(6):
        await r.serve("/v1/completions", _small_body(), {})

    rows = _journal_rows(tmp_path)
    dispatched = [row["prefill_iid"] for row in rows]
    assert dispatched.count("e0") == _EJECT_AFTER, "the dead instance keeps taking dispatch"
    assert r.state()["ejected"] == ["e0"]
    assert rows[-1]["error"] is None, "the fleet serves once the corpse is out"
    await r.engines.aclose()


async def test_an_ejected_instance_is_out_of_the_load_it_reads_zero_for(tmp_path):
    """Arrow §5.5 averages over the instances doing the phase's work. A dead one
    carries nothing, so leaving it in the mean reports a pool at half the
    load it is under and holds Algorithm 2's expand trigger down."""
    stub = StubFleet()
    r = _router(tmp_path, stub, n=6)
    r.monitor.dispatched("e1", Request(rid="hot", input_len=8000, phase=Phase.PREFILL))
    with_dead = r.scheduler.pool_load(Role.PREFILL)

    for _ in range(_EJECT_AFTER):
        r.scheduler.record_failure("e0")

    assert "e0" in r.scheduler.ejected
    assert r.scheduler.pool_load(Role.PREFILL) == pytest.approx(with_dead * 2)
    await r.engines.aclose()


async def test_a_recovered_engine_is_readmitted_by_the_monitor_loop(tmp_path):
    """An ejected instance is dispatched nothing, so nothing it serves can
    prove it well again. The /health probe is the only way back."""
    stub = StubFleet(unreachable={"e0"})
    r = _router(tmp_path, stub, n=6)
    for _ in range(_EJECT_AFTER):
        r.scheduler.record_failure("e0")

    assert await _readmit(r, 0.0) == [], "still refusing the connection"

    stub.unreachable = set()
    assert await _readmit(r, 0.0) == ["e0"]
    assert not r.scheduler.ejected
    assert r.state()["ejected"] == []
    await r.engines.aclose()


async def test_an_idle_fleet_still_finds_out_an_engine_died(tmp_path):
    """The breaker learns from served traffic, so with nothing dispatched a
    dead engine would keep its role forever. The sweep is the traffic-free
    path to the same verdict."""
    stub = StubFleet(unreachable={"e0"})
    r = _router(tmp_path, stub, n=6)
    misses = r.cfg.liveness_misses

    for _ in range(misses - 1):
        assert await _sweep_liveness(r) == [], "one silence is a blip, not a verdict"
        assert "e0" not in r.scheduler.ejected

    assert await _sweep_liveness(r) == ["e0"]
    assert "e0" in r.scheduler.ejected
    assert r.served == 0, "no request was needed to reach the verdict"
    await r.engines.aclose()


async def test_a_blip_between_sweeps_does_not_eject(tmp_path):
    """Consecutive silences are the evidence. An engine that answers again
    starts over, so a single missed probe never accumulates into ejection."""
    stub = StubFleet(unreachable={"e0"})
    r = _router(tmp_path, stub, n=6)

    assert await _sweep_liveness(r) == []
    stub.unreachable = set()
    assert await _sweep_liveness(r) == []
    assert r.scheduler.liveness_misses == {}, "answering cleared the count"

    stub.unreachable = {"e0"}
    for _ in range(r.cfg.liveness_misses - 1):
        assert await _sweep_liveness(r) == []
    assert await _sweep_liveness(r) == ["e0"], "the count restarted rather than carried"
    await r.engines.aclose()


async def test_the_sweep_leaves_the_last_live_instance_alone(tmp_path):
    """The last-live guard is the scheduler's, and the sweep inherits it: a
    router with nowhere to dispatch is worse than one holding a dead engine."""
    stub = StubFleet(unreachable={"e0", "e1"})
    r = _router(tmp_path, stub, n=2)
    for _ in range(r.cfg.liveness_misses + 1):
        await _sweep_liveness(r)
    assert len(r.scheduler.ejected) < 2, "something is always left to schedule onto"
    await r.engines.aclose()


async def test_the_sweep_can_be_turned_off(tmp_path):
    """`liveness_every: 0` restores the traffic-only behaviour."""
    stub = StubFleet(unreachable={"e0"})
    r = _router(tmp_path, stub, n=6)
    r.cfg.liveness_every = 0
    for _ in range(r.cfg.liveness_misses + 1):
        await _monitor_once(r)
    assert "e0" not in r.scheduler.ejected, "the sweep never ran"
    await r.engines.aclose()


async def test_the_last_live_instance_is_never_ejected(tmp_path):
    """Ejecting the whole fleet answers no differently from dispatching into
    it, and leaves nothing to probe from."""
    stub = StubFleet()
    r = _router(tmp_path, stub, n=2)

    for iid in ("e0", "e1"):
        for _ in range(_EJECT_AFTER):
            r.scheduler.record_failure(iid)

    assert sorted(r.scheduler.ejected) == ["e0"]
    assert r.scheduler.schedule(Request(rid="r", input_len=100)).iid == "e1"
    await r.engines.aclose()


async def test_an_engine_that_rejects_the_body_is_not_ejected_for_it(tmp_path):
    """A 4xx names the caller's body, and the engine answered to say so.
    Counting it would eject a healthy fleet under one bad client."""
    stub = StubFleet(refuse_prefill=True)
    r = _router(tmp_path, stub, n=6)

    for _ in range(_EJECT_AFTER + 1):
        await r.serve("/v1/completions", _small_body(), {})

    assert not r.scheduler.ejected
    await r.engines.aclose()


async def test_the_token_count_asks_one_instance_on_its_own_budget(tmp_path):
    """Walking the fleet charges every request the timeout of each wedged
    node it passes; the answerer is cached, not rediscovered."""
    stub = StubFleet()
    r = _router(tmp_path, stub, n=6)

    await r.serve("/v1/completions", _small_body(), {})
    await r.serve("/v1/completions", _small_body(), {})

    assert stub.tokenize_targets == ["e0", "e0"]
    assert stub.tokenize_budgets == [r.cfg.tokenize_timeout_s] * 2
    await r.engines.aclose()


async def test_a_refused_tokenize_probe_moves_to_the_next_instance(tmp_path):
    """The request in hand takes the character estimate, and the one after
    it asks somewhere else."""
    stub = StubFleet(unreachable={"e0"})
    r = _router(tmp_path, stub, n=6)

    await r.serve("/v1/completions", _small_body(), {})
    stub.unreachable = set()
    await r.serve("/v1/completions", _small_body(), {})

    assert stub.tokenize_targets == ["e1"], "the refused instance is asked once"
    await r.engines.aclose()
