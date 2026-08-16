"""The admission door and the queue: what the fleet refuses, and where queued
prefill legs land after a re-placement pass.

The invariant under test: a request serves within its SLO or is refused
immediately with an honest signal. It never queues into a slow death.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from narwhal.bench import score_journal
from narwhal.config import EngineSpec, FleetConfig
from narwhal.journal import RunJournal
from narwhal.profiler import Profile
from narwhal.scheduler import SLO
from narwhal.server import ArrowRouter
from narwhal.types import Request, Role


class Harness:
    """A two-engine stub speaking the split protocol; named engines hang prefill."""

    def __init__(self, hang: frozenset[str] = frozenset()) -> None:
        self.hang = hang
        self.prefill_targets: list[str] = []
        self.decode_targets: list[str] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        iid = f"e{request.url.port - 8101}"
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        body = json.loads(request.content)
        if request.url.path == "/tokenize":
            count = max(1, len(str(body.get("prompt", ""))) // 4)
            return httpx.Response(200, json={"count": count})
        is_prefill = (body.get("kv_transfer_params") or {}).get("do_remote_decode") is True
        if is_prefill:
            self.prefill_targets.append(iid)
            if iid in self.hang:
                await asyncio.Event().wait()  # re-placement cancels this await
            return httpx.Response(
                200,
                json={
                    "id": "c1",
                    "object": "text_completion",
                    "choices": [
                        {
                            "index": 0,
                            "text": "",
                            "kv_transfer_params": {
                                "remote_engine_id": "stub",
                                "remote_block_ids": [1],
                            },
                        }
                    ],
                },
            )
        self.decode_targets.append(iid)
        n = int(body.get("max_tokens", 3))
        lines = [
            "data: "
            + json.dumps(
                {"id": "c1", "object": "text_completion.chunk", "choices": [{"text": f"t{k}"}]}
            )
            for k in range(n)
        ]
        lines.append("data: [DONE]")
        return httpx.Response(200, text="\n".join(lines) + "\n")


def _profiles(ttft_c: float = 0.005) -> dict[str, Profile]:
    """prefill_time(n) ~= 1.75 s at 15k tokens on this curve."""
    return {
        e: Profile(
            iid=e, ttft_a=2e-8, ttft_b=6e-5, ttft_c=ttft_c, tpot_slope=3e-6, tpot_intercept=0.012
        )
        for e in ("e0", "e1")
    }


def _router(tmp_path: Path, stub: Harness, profiles: dict[str, Profile], **kw) -> ArrowRouter:
    cfg = FleetConfig(
        model="stub-model",
        engines=[
            EngineSpec(iid="e0", url="http://127.0.0.1:8101", role=Role.PREFILL),
            EngineSpec(iid="e1", url="http://127.0.0.1:8102", role=Role.PREFILL),
        ],
        slo=SLO(ttft_s=10.0, tpot_s=0.125),
        profiles_path=tmp_path / "profiles.json",
        **kw,
    )
    journal = RunJournal(path=tmp_path / "journal.jsonl")
    journal.open()
    router = ArrowRouter(cfg, journal, transport=stub.transport())
    for profile in profiles.values():
        router.profiles.put(profile)
    return router


def _body(prompt: str = "x" * 40, max_tokens: int = 4) -> dict:
    return {"prompt": prompt, "max_tokens": max_tokens, "stream": False}


def _rows(tmp_path: Path) -> list[dict]:
    return [
        r
        for x in (tmp_path / "journal.jsonl").read_text().splitlines()
        if x.strip() and "meta" not in (r := json.loads(x))
    ]


async def test_a_queue_the_fleet_cannot_drain_in_time_is_refused_with_a_retry(tmp_path):
    """Work ahead of the request prices it out, and that work drains: 429 in
    milliseconds, an honest Retry-After, accounted apart from failures."""
    stub = Harness()
    router = _router(tmp_path, stub, _profiles())
    # Two 15k-token legs on each engine price every landing at ~10.8 s against
    # the 10 s budget. The offered prompt is 10 tokens: it is the queue, not
    # the prompt, that misses.
    for iid in ("e0", "e1"):
        for k in range(2):
            router.monitor.dispatched(iid, Request(rid=f"{iid}-ahead{k}", input_len=15_000))

    resp = await router.serve("/v1/completions", _body(), {})

    assert resp.status_code == 429
    assert resp.headers["x-request-id"]
    assert int(resp.headers["retry-after"]) >= 1  # the priced overrun, drained
    assert json.loads(resp.body)["error"]["type"] == "server_overloaded_error"
    assert b"retry as the priced queue drains" in resp.body
    assert stub.prefill_targets == [], "no prefill leg dispatches"
    assert stub.decode_targets == [], "no decode leg dispatches"
    assert router.refused == 1
    assert router.failed == 0
    assert router.served == 0
    (row,) = _rows(tmp_path)
    assert row["refused"] is True
    assert row["refused_cause"] == "queue"
    assert row["error"].startswith("refused: cheapest placement")
    assert row["input_len"] > 0, "the oracle prices offered work, refused included"
    assert router.state()["admission"]["refused"] == 1
    await router.engines.aclose()


async def test_a_prompt_over_budget_alone_is_refused_without_a_retry(tmp_path):
    """An empty fleet already prices this prompt past the budget. Nothing
    drains into servable, so the door quotes no Retry-After and says why."""
    heavy = _profiles(ttft_c=50.0)  # even an empty engine prices 50 s of prefill
    stub = Harness()
    router = _router(tmp_path, stub, heavy)

    resp = await router.serve("/v1/completions", _body(), {})

    assert resp.status_code == 429
    assert resp.headers["x-request-id"]
    assert "retry-after" not in resp.headers, "no wait makes this prompt servable"
    assert b"shorten the prompt or raise the TTFT budget" in resp.body
    assert stub.prefill_targets == [], "no prefill leg dispatches"
    assert router.refused == 1
    assert router.served == 0
    (row,) = _rows(tmp_path)
    assert row["refused"] is True
    assert row["refused_cause"] == "prompt"
    assert row["error"].startswith("refused: this prompt's own prefill")
    assert router.state()["admission"]["refused"] == 1
    await router.engines.aclose()


async def test_what_fits_is_admitted_as_before(tmp_path):
    stub = Harness()
    router = _router(tmp_path, stub, _profiles())
    resp = await router.serve("/v1/completions", _body(), {})
    assert resp.status_code == 200
    assert router.served == 1
    assert router.refused == 0
    assert not any(r.get("refused") for r in _rows(tmp_path))
    await router.engines.aclose()


async def test_open_admission_keeps_the_old_door(tmp_path):
    """The paired arm: always admit, whatever the cost model says."""
    stub = Harness()
    router = _router(tmp_path, stub, _profiles(ttft_c=50.0), admission="open")
    resp = await router.serve("/v1/completions", _body(), {})
    assert resp.status_code == 200
    assert router.refused == 0
    await router.engines.aclose()


async def test_the_margin_is_hysteresis_around_the_budget(tmp_path):
    """Priced 10.4 s against a 10 s budget: a miss at margin 0, noise at 0.1."""
    on_the_line = _profiles(ttft_c=10.39)  # + the stub's 10-token prompt ≈ 10.4
    strict = _router(tmp_path / "a", Harness(), on_the_line, admission_margin=0.0)
    resp = await strict.serve("/v1/completions", _body(), {})
    assert resp.status_code == 429
    await strict.engines.aclose()

    slack = _router(tmp_path / "b", Harness(), on_the_line, admission_margin=0.1)
    resp = await slack.serve("/v1/completions", _body(), {})
    assert resp.status_code == 200, f"10.4 <= 10 * 1.1 must admit: {resp.body}"
    await slack.engines.aclose()


async def test_a_batched_window_refuses_only_its_dead_work(tmp_path):
    """Two requests share a window: the one no placement can save is refused,
    the savable one is placed - never dead work ahead of live work."""
    stub = Harness()
    profiles = _profiles()
    profiles["e0"] = Profile(
        iid="e0", ttft_a=2e-8, ttft_b=6e-5, ttft_c=50.0, tpot_slope=3e-6, tpot_intercept=0.012
    )
    router = _router(
        tmp_path,
        stub,
        profiles,
        placement="batched",
        batch_window_ms=30_000,  # batch_max flushes first: the test stays fast
        batch_max=2,
    )
    small = asyncio.create_task(router.serve("/v1/completions", _body(), {}))
    await asyncio.sleep(0)
    doomed = asyncio.create_task(router.serve("/v1/completions", _body(prompt="y" * 1_000_000), {}))
    small_resp, doomed_resp = await asyncio.gather(small, doomed)

    assert small_resp.status_code == 200
    assert doomed_resp.status_code == 429
    assert stub.prefill_targets == ["e1"], "only the savable request dispatches"
    assert router.served == 1
    assert router.refused == 1
    # The gate refuses on the joint assignment, and the door still splits the
    # cause: a million characters is over budget on an empty fleet.
    (refusal,) = [r for r in _rows(tmp_path) if r.get("refused")]
    assert refusal["refused_cause"] == "prompt"
    assert "retry-after" not in doomed_resp.headers
    await router.engines.aclose()


async def test_a_leg_priced_out_of_its_queue_is_re_driven_elsewhere(tmp_path):
    """e0 hangs the leg and its queue prices over budget; the pass cancels the
    dispatch and the serve loop lands the leg on the drained peer."""
    stub = Harness(hang=frozenset({"e0"}))
    router = _router(tmp_path, stub, _profiles())
    serve = asyncio.create_task(router.serve("/v1/completions", _body(), {}))
    for _ in range(500):
        await asyncio.sleep(0)
        if stub.prefill_targets:
            break
    assert stub.prefill_targets == ["e0"], "the leg never hung at e0"
    assert router._legs

    # Three 15k-token legs land behind it: staying on e0 now prices at ~29 s.
    for k in range(3):
        router.monitor.dispatched("e0", Request(rid=f"behind{k}", input_len=15_000))
    moved = router.apply_queue_replacements()
    assert moved == 1

    resp = await asyncio.wait_for(serve, timeout=5)
    assert resp.status_code == 200
    assert stub.prefill_targets == ["e0", "e1"], "cancelled at e0, re-driven at e1"
    assert router.refused == 0
    assert router.failed == 0
    assert router.served == 1
    (row,) = _rows(tmp_path)
    assert row["error"] is None
    assert row["prefill_iid"] == "e1"
    await router.engines.aclose()


async def test_a_pass_over_idle_queues_moves_nothing(tmp_path):
    router = _router(tmp_path, Harness(), _profiles())
    assert router.apply_queue_replacements() == 0
    await router.engines.aclose()


def test_refused_rows_stay_out_of_the_attainment_denominator(tmp_path):
    """The scorer counts offered work that could have served; refusals are a
    separate KPI, or overload reads as request deaths it never caused."""
    journal = tmp_path / "j.jsonl"
    rows = [
        {
            "rid": "ok",
            "input_len": 10,
            "output_len": 4,
            "wanted_len": 4,
            "ttft_s": 1.0,
            "tpot_s": 0.05,
            "error": None,
        },
        {"rid": "no", "input_len": 10, "output_len": 0, "error": "refused: ...", "refused": True},
    ]
    journal.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert score_journal(journal, ttft_slo=10.0, tpot_slo=0.125) == (1.0, 1, 1)
