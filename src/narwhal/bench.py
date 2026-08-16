"""Drive load at the router and score what came back against §6.1's metric.

Sweeps request rate and reports the highest that holds the attainment target.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .engine import sse_token_count
from .provenance import stamp_line
from .trace import (
    FILLER,
    PHASE_SECONDS,
    SEGMENTS,
    Segments,
    _prompt_of,
    derive_segments,
    load_trace,
    make_trace,
    parse_segments,
    scale_segments,
    set_tokens_per_repeat,
)


@dataclass
class Sample:
    """One request as the client measured it; §6.1's metric scores these."""

    rid: str
    input_len: int
    output_len: int
    ttft_s: float | None
    tpot_s: float | None
    error: str | None
    wanted_len: int = 0


async def _one(
    client: httpx.AsyncClient,
    base: str,
    model: str,
    idx: int,
    isl: int,
    osl: int,
    prefix: tuple[int, int] | None = None,
) -> Sample:
    prompt = _prompt_of(isl, prefix)
    body = {
        "model": model,
        "prompt": prompt,
        "max_tokens": osl,
        "min_tokens": osl,
        "temperature": 0.0,
        "ignore_eos": True,
        "stream": True,
    }
    start = time.monotonic()
    first: float | None = None
    tokens = 0
    try:
        async with client.stream("POST", f"{base}/v1/completions", json=body) as r:
            if r.status_code != 200:
                detail = (await r.aread()).decode("utf-8", "replace")[:200]
                return Sample(f"b{idx}", isl, 0, None, None, f"http {r.status_code}: {detail}", osl)
            async for line in r.aiter_lines():
                n = sse_token_count(line)
                if not n:
                    continue
                if first is None:
                    first = time.monotonic()
                tokens += n
    except Exception as exc:
        return Sample(f"b{idx}", isl, tokens, None, None, str(exc), osl)
    done = time.monotonic()
    return Sample(
        rid=f"b{idx}",
        input_len=isl,
        output_len=tokens,
        ttft_s=(first - start) if first else None,
        tpot_s=((done - first) / (tokens - 1)) if first and tokens > 1 else None,
        error=None,
        wanted_len=osl,
    )


CALIBRATION_REPEATS = 1000


async def calibrate_filler(client: httpx.AsyncClient, base: str, model: str) -> float | None:
    """Measure the serving tokenizer's cost of the filler, through the router.

    One non-streaming completion; `usage.prompt_tokens` is the engine's own
    count. None on any failure - the caller keeps the default ratio
    rather than sizing every prompt off a bad measurement.
    """
    body = {
        "model": model,
        "prompt": FILLER * CALIBRATION_REPEATS,
        "max_tokens": 1,
        "temperature": 0.0,
        "stream": False,
    }
    try:
        r = await client.post(f"{base}/v1/completions", json=body, timeout=120.0)
        if r.status_code != 200:
            return None
        tokens = r.json()["usage"]["prompt_tokens"]
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None
    return tokens / CALIBRATION_REPEATS if tokens else None


async def drive(
    base: str,
    model: str,
    rate: float,
    seed: int,
    phase_seconds: float = PHASE_SECONDS,
    segments: Segments | None = None,
    trace_path: Path | None = None,
    tokens_per_repeat: float = 0.0,
) -> list[Sample]:
    """Replay a trace against the router and gather every Sample.

    Prompt sizing calibrates against the serving tokenizer first (one
    non-streaming request through the router); pass `tokens_per_repeat` to
    skip the measurement and use a known ratio.
    """
    if trace_path is not None:
        trace = load_trace(trace_path, rate)
    else:
        trace = [
            (at, isl, osl, None)
            for (at, isl, osl) in make_trace(
                rate, seed, segments or scale_segments(SEGMENTS, phase_seconds)
            )
        ]
    limits = httpx.Limits(max_connections=1024, max_keepalive_connections=512)
    async with httpx.AsyncClient(timeout=httpx.Timeout(900.0, connect=10.0), limits=limits) as c:
        if tokens_per_repeat > 0.0:
            set_tokens_per_repeat(tokens_per_repeat)
        else:
            ratio = await calibrate_filler(c, base, model)
            if ratio is not None:
                set_tokens_per_repeat(ratio)
                print(f"  filler calibrated: {ratio:.3f} tokens/repeat", file=sys.stderr)
            else:
                print(
                    "  filler calibration failed; keeping the reference ratio",
                    file=sys.stderr,
                )
        start = time.monotonic()
        tasks = []
        for idx, (at, isl, osl, prefix) in enumerate(trace):
            delay = at - (time.monotonic() - start)
            if delay > 0:
                await asyncio.sleep(delay)
            tasks.append(asyncio.create_task(_one(c, base, model, idx, isl, osl, prefix)))
        return list(await asyncio.gather(*tasks))


def score(samples: list[Sample], ttft_slo: float, tpot_slo: float) -> tuple[float, int, int]:
    """Fraction of offered requests that came back whole and met both targets.

    Three ways to miss: an error, a target overrun, or a short response.

    TTFT here is the client's wait for its first byte. `narwhal-report` scores the
    router's journal against Arrow §4.2's `q1 + p1`, so the two differ by the KV
    transfer.
    """
    total = len(samples)
    if not total:
        return 0.0, 0, 0
    met = sum(
        1
        for s in samples
        if s.error is None
        and s.output_len >= s.wanted_len
        and s.ttft_s is not None
        and s.ttft_s <= ttft_slo
        and (s.tpot_s is None or s.tpot_s <= tpot_slo)
    )
    return met / total, met, total


def score_journal(path: Path, ttft_slo: float, tpot_slo: float) -> tuple[float, int, int]:
    """Same metric, taken from the router's own journal instead of the client.

    Refused rows stay out: a request refused at the door was never served
    late, and mixing it into the denominator would charge the arm for the
    honesty. The rerun counters report refusals beside this score.
    """
    rows = [
        r
        for line in path.read_text().splitlines()
        if line.strip() and "meta" not in (r := json.loads(line)) and not r.get("refused")
    ]
    samples = [
        Sample(
            rid=r["rid"],
            input_len=r["input_len"],
            output_len=r["output_len"],
            ttft_s=r["ttft_s"],
            tpot_s=r["tpot_s"],
            error=r["error"],
            wanted_len=r.get("wanted_len", 0),
        )
        for r in rows
    ]
    return score(samples, ttft_slo, tpot_slo)


async def _already_driven(base: str, settle_s: float = 1.5) -> str:
    """Describe traffic the router is carrying for someone else, or "".

    The fleet has one router port, so an arm launched over a running one
    measures both. Resident work is the immediate signal: `served` counts
    completions, and at a low rate with long generations a short window sees
    none while the router is busy.
    """
    async with httpx.AsyncClient(timeout=8.0) as c:
        try:
            first = (await c.get(f"{base}/arrow/state")).json()
            await asyncio.sleep(settle_s)
            second = (await c.get(f"{base}/arrow/state")).json()
        except (httpx.HTTPError, KeyError, ValueError):
            return ""
    resident = sum(v["prefill"] + v["decode"] for v in second.get("resident", {}).values())
    completed = max(0, second.get("served", 0) - first.get("served", 0))
    if resident:
        return f"{resident} requests resident"
    if completed:
        return f"{completed} requests completed while this process was idle"
    return ""


def _derived_segments(profiles: Path, tpot_slo: float, phase_seconds: float) -> Segments | None:
    """Fleet-priced default phases, or None to keep the reference numbers."""
    if not profiles.exists():
        return None
    from .profiler import ProfileStore

    derived = derive_segments(ProfileStore(profiles), tpot_slo, phase_seconds)
    if derived is not None:
        mults = ", ".join(f"{s[-1]:g}x" for s in derived)
        print(f"  phase multipliers from {profiles}: {mults}", file=sys.stderr)
    return derived


def main(argv: list[str] | None = None) -> int:
    """CLI: sweep rates or replay a trace, and report attainment per rate."""
    ap = argparse.ArgumentParser(description="Sweep request rate against a router (Arrow §6.1)")
    ap.add_argument("--base", default="http://127.0.0.1:8000", help="router base URL")
    ap.add_argument("--model", help="required to drive load; --score-journal needs only the SLOs")
    ap.add_argument("--ttft-slo", type=float, required=True)
    ap.add_argument("--tpot-slo", type=float, required=True)
    ap.add_argument(
        "--rates",
        default="0.6,1.0,1.6,2.4,3.2",
        help="comma-separated request rates to sweep",
    )
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--target", type=float, default=0.90)
    ap.add_argument("--out", default="", help="write per-request rows here as JSONL")
    ap.add_argument(
        "--score-journal",
        default="",
        help="score a router journal against the SLOs instead of driving load",
    )
    ap.add_argument("--force", action="store_true", help="drive even if the fleet is busy")
    ap.add_argument(
        "--tokens-per-repeat",
        type=float,
        default=0.0,
        help="filler tokens per repetition; 0 (default) measures it against "
        "the serving tokenizer at startup",
    )
    ap.add_argument(
        "--phase-seconds",
        type=float,
        default=PHASE_SECONDS,
        help="length of each built-in phase; --segments carries its own durations and ignores this",
    )
    ap.add_argument(
        "--segments",
        default="",
        help="replace the built-in phases: dur:isl_lo-isl_hi:osl_lo-osl_hi:mult, "
        "comma-separated; durations are taken as written",
    )
    ap.add_argument(
        "--profiles",
        default="runs/local/profiles.json",
        help="profile store; when present, the built-in phases' rate "
        "multipliers are priced from the fleet's own fitted curves rather "
        "than the shipped fallbacks. --segments and --trace-file "
        "bypass it",
    )
    ap.add_argument(
        "--trace-file",
        default="",
        help="replay timestamped JSONL {at, input_len, output_len}; --rates "
        "becomes §6.1's timestamp constant and the generator is bypassed",
    )
    args = ap.parse_args(argv)

    if args.score_journal:
        frac, met, total = score_journal(Path(args.score_journal), args.ttft_slo, args.tpot_slo)
        refused = sum(
            1
            for line in Path(args.score_journal).read_text().splitlines()
            if line.strip() and json.loads(line).get("refused")
        )
        print(f"{frac * 100:.1f}% attainment, {met}/{total}, {refused} refused")
        return 0
    if not args.model:
        ap.error("--model is required to drive load")

    busy = asyncio.run(_already_driven(args.base))
    if busy and not args.force:
        print(
            f"{args.base} has {busy}, so another arm is driving the same fleet. "
            f"Both arms would measure the other's load. Stop it, or pass --force.",
            file=sys.stderr,
        )
        return 2

    rates = [float(x) for x in args.rates.split(",") if x.strip()]
    print(f"{'rate':>6}  {'attainment':>11}  {'met/total':>12}")
    sustained = 0.0
    out = Path(args.out) if args.out else None
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(stamp_line())
    segments = parse_segments(args.segments) if args.segments else None
    trace_path = Path(args.trace_file) if args.trace_file else None
    if segments is None and trace_path is None:
        segments = _derived_segments(Path(args.profiles), args.tpot_slo, args.phase_seconds)
    for rate in rates:
        samples = asyncio.run(
            drive(
                args.base,
                args.model,
                rate,
                args.seed,
                args.phase_seconds,
                segments=segments,
                trace_path=trace_path,
                tokens_per_repeat=args.tokens_per_repeat,
            )
        )
        frac, met, total = score(samples, args.ttft_slo, args.tpot_slo)
        print(f"{rate:>6g}  {frac * 100:>10.1f}%  {met:>5}/{total:<6}")
        if out:
            with out.open("a") as fh:
                for s in samples:
                    fh.write(json.dumps({"rate": rate, **s.__dict__}) + "\n")
        if frac >= args.target:
            sustained = rate
    shown = f"{sustained:g} req/s" if sustained else "none"
    print(f"\nsustained at {args.target:.0%} attainment: {shown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
