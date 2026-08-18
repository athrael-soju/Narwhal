"""The offline profiler, driven against tools/stub_fleet.py.

The stub answers /tokenize and streams on Arrow §3.1's timing model, so both fits
have a known curve to recover. It runs in its own process because the decode
sweep measures inter-token gaps, which an in-process transport flattens.
"""

from __future__ import annotations

import asyncio
import importlib.util
import multiprocessing
import socket
import time
from pathlib import Path

import httpx
import pytest

from narwhal import probe

MODEL = "stub"
# probe_decode's own prompt target, from its make_prompt call.
DECODE_PROMPT_TOKENS = 512


def _load_stub():
    """tools/ ships no package, so the stub loads by path."""
    path = Path(__file__).resolve().parents[1] / "tools" / "stub_fleet.py"
    spec = importlib.util.spec_from_file_location("stub_fleet", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


stub_fleet = _load_stub()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_health(url: str, deadline_s: float = 15.0) -> None:
    end = time.monotonic() + deadline_s
    with httpx.Client(timeout=1.0) as client:
        while time.monotonic() < end:
            try:
                if client.get(f"{url}/health").status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
    raise RuntimeError(f"the stub never answered /health on {url}")


def _stop(proc) -> None:
    """Terminate the stub, and kill it if it ignores the signal."""
    proc.terminate()
    proc.join(timeout=5)
    if proc.is_alive():
        proc.kill()


@pytest.fixture(scope="module")
def stub_url():
    # `_free_port` has to release the port for the stub's subprocess to bind
    # it, so unlike the check-gate tests this window cannot be closed by
    # holding the socket. Another process taking the port in between shows up
    # as a stub that never answers, which is indistinguishable here from a
    # stub that failed to start. Retry on a fresh port instead: the collision
    # is independent per attempt, so a second one clears it.
    last: Exception | None = None
    for _ in range(3):
        port = _free_port()
        proc = multiprocessing.get_context("fork").Process(
            target=stub_fleet._serve_one, args=("e0", MODEL, port), daemon=True
        )
        proc.start()
        url = f"http://127.0.0.1:{port}"
        try:
            _wait_for_health(url)
        except RuntimeError as exc:
            last = exc
            _stop(proc)
            continue
        try:
            yield url
        finally:
            _stop(proc)
        return
    assert last is not None
    raise last


SHORT = probe.Sweep(
    prefill_lens=(256, 512, 1024, 2048),
    prefill_repeats=2,
    decode_concurrency=(1, 4, 12),
    decode_tokens=8,
)


async def _profile(url: str):
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=5.0)) as client:
        return await probe.profile_instance(client, "e0", url, MODEL, SHORT)


@pytest.fixture(scope="module")
def probed(stub_url):
    """One profiling pass on a short sweep, with the decode samples it fitted.

    The shipped sweep runs 18 prefill requests up to 8192 tokens and 69
    concurrent decode streams, which is a minute of sleeping.
    """
    decode: dict[str, list[tuple[float, float]]] = {}
    with pytest.MonkeyPatch.context() as mp:
        sweep = probe.probe_decode

        async def recording(client, url, model, concurrency, tokens, *rest):
            decode["samples"] = await sweep(client, url, model, concurrency, tokens, *rest)
            return decode["samples"]

        mp.setattr(probe, "probe_decode", recording)
        yield asyncio.run(_profile(stub_url)), decode["samples"]


async def test_the_recorded_length_is_the_engines_own_count(stub_url):
    async with httpx.AsyncClient(timeout=10.0) as client:
        text, n = await probe.make_prompt(client, stub_url, MODEL, 512)
        r = await client.post(f"{stub_url}/tokenize", json={"model": MODEL, "prompt": text})
    assert n == r.json()["count"]
    assert abs(n - 512) <= 26


async def test_the_engines_count_wins_over_the_requested_length():
    """A tokenizer that caps the count fixes the x value at the cap."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"count": 1024})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        _, n = await probe.make_prompt(client, "http://e0", MODEL, 4096)
    assert n == 1024


async def test_a_missing_tokenize_endpoint_stops_the_probe():
    """Without a measured x axis the fit describes a curve nobody measured."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="Not Found")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError) as err:
            await probe.make_prompt(client, "http://e0", MODEL, 512)
    assert "http://e0" in str(err.value)
    assert "404" in str(err.value)


async def test_an_unreachable_tokenize_endpoint_stops_the_probe():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError) as err:
            await probe.make_prompt(client, "http://e0", MODEL, 512)
    assert "http://e0" in str(err.value)


async def test_a_tokenize_body_without_a_count_stops_the_probe():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tokens": [1, 2, 3]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="no count"):
            await probe.make_prompt(client, "http://e0", MODEL, 512)


async def test_a_failed_correction_call_stops_the_probe():
    """The correction call raises too, so neither call can seed an estimated x."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={"count": 1300})
        return httpx.Response(500, text="tokenizer busy")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="500"):
            await probe.make_prompt(client, "http://e0", MODEL, 512)
    assert calls["n"] == 2


def test_the_prefill_fit_recovers_the_stubs_quadratic(probed):
    profile, _ = probed
    for n in probe.PREFILL_LENS:
        truth = stub_fleet.TTFT_A * n * n + stub_fleet.TTFT_B * n + stub_fleet.TTFT_C
        assert profile.prefill_time(n) == pytest.approx(truth, rel=0.25)


def test_the_decode_x_axis_is_the_shared_batch(probed):
    """Arrow §3.1 prices decode against the tokens resident on the instance.

    Twelve concurrent streams of 512-token prompts put about 6,100 tokens on
    the engine. A per-stream x axis would top out near 520.
    """
    _, decode = probed
    batches = [x for x, _ in decode]
    assert max(batches) > 8 * DECODE_PROMPT_TOKENS
    assert min(batches) < 1.5 * DECODE_PROMPT_TOKENS


def test_the_decode_fit_recovers_the_batch_slope(probed):
    """Only the slope. The intercept carries the harness's own per-token floor."""
    profile, _ = probed
    assert profile.tpot_slope == pytest.approx(stub_fleet.TPOT_SLOPE, rel=0.5)
