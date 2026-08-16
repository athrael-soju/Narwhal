"""The trace generator's two new inputs: segment specs and recorded traces."""

from __future__ import annotations

import json

import pytest

from narwhal.trace import load_trace, make_trace, parse_segments


def test_a_segment_spec_round_trips():
    spec = "300:8000-16000:30-60:4.4, 240:300-800:2000-8000:0.4"
    assert parse_segments(spec) == (
        (300.0, (8000, 16000), (30, 60), 4.4),
        (240.0, (300, 800), (2000, 8000), 0.4),
    )


@pytest.mark.parametrize("bad", ["", "300:8000-16000:30-60", "x:1-2:3-4:5", "1:2:3:4"])
def test_a_malformed_segment_names_the_shape(bad):
    with pytest.raises(ValueError, match=r"is not dur:|no segments"):
        parse_segments(bad)


def test_arrivals_stay_inside_their_phase_and_bands():
    segments = parse_segments("100:1000-2000:10-20:2.0,50:100-200:500-600:1.0")
    trace = make_trace(rate=1.0, seed=7, segments=segments)
    assert trace, "two phases at these rates produce arrivals"
    for at, isl, osl in trace:
        if at < 100.0:
            assert 1000 <= isl <= 2000
            assert 10 <= osl <= 20
        else:
            assert at < 150.0, "nothing arrives past the last phase"
            assert 100 <= isl <= 200
            assert 500 <= osl <= 600


def test_the_multiplier_scales_a_phases_arrivals():
    lo = make_trace(1.0, 7, parse_segments("200:100-200:10-20:1.0"))
    hi = make_trace(1.0, 7, parse_segments("200:100-200:10-20:8.0"))
    assert len(hi) > 4 * len(lo), "8x the rate is many times the arrivals"


def test_a_recorded_trace_replays_at_the_timestamp_constant(tmp_path):
    """§6.1: "we multiply the timestamps by a constant to simulate varying
    request rates". Rate 2.0 arrives in half the recorded time."""
    p = tmp_path / "trace.jsonl"
    rows = [
        {"at": 0.0, "input_len": 1500, "output_len": 13},
        {"at": 10.0, "input_len": 1020, "output_len": 129},
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    replay = load_trace(p, rate=2.0)
    assert replay == [(0.0, 1500, 13, None), (5.0, 1020, 129, None)]


def test_a_shared_prefix_trace_rides_its_identity_through_replay(tmp_path):
    """Shared-prefix traces carry prefix_id/prefix_len; replay keeps them and the
    prompt synthesizer gives same-prefix requests identical heads that
    differ from every other prefix within the first characters."""
    import json as _json

    from narwhal.trace import _prompt_of

    p = tmp_path / "trace.jsonl"
    rows = [
        {"at": 0.0, "input_len": 9000, "output_len": 60, "prefix_id": 1, "prefix_len": 8000},
        {"at": 1.0, "input_len": 8500, "output_len": 60, "prefix_id": 1, "prefix_len": 8000},
        {"at": 2.0, "input_len": 9000, "output_len": 60, "prefix_id": 2, "prefix_len": 8000},
    ]
    p.write_text("\n".join(_json.dumps(r) for r in rows) + "\n")
    replay = load_trace(p, rate=1.0)
    assert replay[0][3] == (1, 8000)
    assert replay[2][3] == (2, 8000)

    a = _prompt_of(9000, (1, 8000))
    b = _prompt_of(8500, (1, 8000))
    c = _prompt_of(9000, (2, 8000))
    shared = len("ctx1 ") * 8000  # the whole prefix block
    assert a[: len(b)][:shared] == b[:shared], "same prefix, same head bytes"
    assert a[:16] != c[:16], "different prefixes diverge immediately"
    assert _prompt_of(500) == _prompt_of(500, None), "no prefix, old behaviour"


def test_the_filler_ratio_is_measured_not_assumed(monkeypatch):
    """Prompt sizing calibrates against the serving tokenizer. A fleet
    whose tokenizer costs half a token per repeat gets prompts twice as long."""
    import httpx

    from narwhal import trace
    from narwhal.bench import CALIBRATION_REPEATS, calibrate_filler

    monkeypatch.setattr(trace, "_tokens_per_repeat", trace.TOKENS_PER_REPEAT)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"usage": {"prompt_tokens": CALIBRATION_REPEATS // 2}, "choices": []}
        )

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await calibrate_filler(c, "http://router", "m")

    import asyncio

    ratio = asyncio.run(_run())
    assert ratio == 0.5
    trace.set_tokens_per_repeat(ratio)
    sized = trace._prompt_of(100)
    assert sized.count(trace.FILLER) == 200, "half a token per repeat doubles the repeats"
    trace.set_tokens_per_repeat(trace.TOKENS_PER_REPEAT)


def test_a_failed_calibration_keeps_the_default(monkeypatch):
    import asyncio

    import httpx

    from narwhal import trace
    from narwhal.bench import calibrate_filler

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await calibrate_filler(c, "http://router", "m")

    assert asyncio.run(_run()) is None
    trace.set_tokens_per_repeat(-1.0)  # non-positive is refused, not applied
    assert trace._tokens_per_repeat == trace.TOKENS_PER_REPEAT


def test_segment_multipliers_price_from_the_fleets_own_fits(tmp_path):
    """The 16x prefill-phase multiplier is one measured fleet's knee
    ratio; a fleet with different curves derives its own."""
    from pathlib import Path

    from narwhal.profiler import Profile, ProfileStore
    from narwhal.trace import SEGMENTS, derive_segments

    store = ProfileStore(Path(tmp_path) / "p.json")
    # Linear prefill at 0.1 ms/token, decode ceiling (0.05-0.02)/3e-7 = 100k
    # resident tokens, so the knees are analytic: the prefill-heavy band is
    # prefill-bound at 1/1.2 s, the decode-heavy band holds 100k/3050
    # streams retiring 5000 tokens at the 50 ms gap = 0.131 req/s, and the
    # ratio is 6.35.
    store.put(
        Profile(iid="e0", ttft_a=0.0, ttft_b=1e-4, ttft_c=0.0, tpot_slope=3e-7, tpot_intercept=0.02)
    )
    derived = derive_segments(store, 0.05, phase_seconds=60.0)
    assert derived is not None
    durs = [s[0] for s in derived]
    bands = [(s[1], s[2]) for s in derived]
    mults = [s[3] for s in derived]
    assert durs == [60.0, 60.0, 60.0]
    assert bands == [(s[1], s[2]) for s in SEGMENTS], "bands stay; only rates re-price"
    assert mults[1] == 1.0
    assert mults[0] == pytest.approx(6.4, abs=0.1)
    assert mults[2] == pytest.approx(mults[0] ** 0.5, abs=0.1)


def test_an_empty_store_keeps_the_reference_multipliers(tmp_path):
    from pathlib import Path

    from narwhal.profiler import ProfileStore
    from narwhal.trace import derive_segments

    assert derive_segments(ProfileStore(Path(tmp_path) / "none.json"), 0.05) is None
