"""The offline profiler driver, §5.2.

A startup job, run per instance, fitting the two curves Arrow §3.1 names: prefill
quadratic in input length, decode linear in batch tokens. Arrow §5.2 caches the result
to disk and re-profiles only the instance whose capability changed.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from dataclasses import dataclass

import httpx

from .config import FleetConfig
from .dialect import EngineDialect, VllmDialect
from .dialect import lookup as lookup_dialect
from .engine import sse_token_count
from .profiler import Profile, ProfileStore, fit_linear, fit_quadratic

# Defaults for the sweep knobs below: spread across the quadratic's
# range - a short-prompt fit extrapolates badly - and wide enough to cover the
# reference trace's 12-16k ISL band, so the fit interpolates where the fleet
# actually runs. Override per fleet with --prefill-lens when the band differs.
PREFILL_LENS = (256, 512, 1024, 2048, 4096, 8192, 12288, 16384)
# Concurrency levels for the decode sweep.
DECODE_CONCURRENCY = (1, 4, 16, 48)
DECODE_TOKENS = 64
PREFILL_REPEATS = 3


async def _tokenize(
    client: httpx.AsyncClient, url: str, model: str, prompt: str, dialect: EngineDialect
) -> int:
    """The engine's own token count for `prompt`.

    Token count is the x axis of both fits, so an estimate here produces a
    plausible profile of a curve nobody measured. Every failure raises: the
    router may fall back to a character ratio at run time, the profiler must
    not - unless the dialect has no route at all, which make_prompt handles
    before this function is ever reached.
    """
    path = dialect.tokenize_path
    if path is None:
        raise RuntimeError(f"the {dialect.name} dialect has no exact-count route")
    try:
        r = await client.post(
            f"{url}{path}",
            json=dialect.tokenize_request(model, {"prompt": prompt}),
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"tokenize probe failed on {url}: {exc}") from exc
    if r.status_code != 200:
        raise RuntimeError(f"tokenize probe failed on {url} ({r.status_code}): {r.text[:200]}")
    try:
        body = r.json()
    except ValueError as exc:
        raise RuntimeError(f"tokenize probe returned no count on {url}: {r.text[:200]}") from exc
    count = dialect.tokenize_response(body)
    if count is None:
        raise RuntimeError(f"tokenize probe returned no count on {url}: {r.text[:200]}")
    if count < 1:
        raise RuntimeError(f"tokenize probe counted {count} tokens on {url}")
    return count


async def make_prompt(
    client: httpx.AsyncClient,
    url: str,
    model: str,
    target: int,
    dialect: EngineDialect | None = None,
    chars_per_token: float = 3.8,
) -> tuple[str, int]:
    """A prompt of about `target` tokens, measured by the engine's tokenizer.

    A dialect without an exact-count route sizes the prompt by the character
    ratio and returns that estimate as the count: the x axis of both fits is
    then approximate, which the caller announces (run does, per instance) -
    the alternative is narwhal-profile refusing to run at all."""
    word = "benchmark "
    dialect = dialect or VllmDialect()
    if dialect.tokenize_path is None:
        text = (word * max(1, target))[: max(1, int(target * chars_per_token))]
        return text, max(1, round(len(text) / chars_per_token))
    text = word * max(1, target)
    got = await _tokenize(client, url, model, text, dialect)
    if got != target:
        scaled = max(1, int(len(text) * target / got))
        text = text[:scaled]
        got = await _tokenize(client, url, model, text, dialect)
    return text, got


async def probe_prefill(
    client: httpx.AsyncClient,
    url: str,
    model: str,
    lens: tuple[int, ...] = PREFILL_LENS,
    repeats: int = PREFILL_REPEATS,
    dialect: EngineDialect | None = None,
    chars_per_token: float = 3.8,
) -> list[tuple[float, float]]:
    """One request at a time, `max_tokens` 1: wall time is prefill time."""
    samples: list[tuple[float, float]] = []
    for target in lens:
        prompt, n = await make_prompt(client, url, model, target, dialect, chars_per_token)
        for _ in range(repeats):
            body = {
                "model": model,
                "prompt": prompt,
                "max_tokens": 1,
                "temperature": 0.0,
                "stream": False,
            }
            start = time.monotonic()
            r = await client.post(f"{url}/v1/completions", json=body, timeout=300.0)
            elapsed = time.monotonic() - start
            if r.status_code != 200:
                raise RuntimeError(
                    f"prefill probe failed on {url} ({r.status_code}): {r.text[:200]}"
                )
            samples.append((float(n), elapsed))
        print(f"    prefill {n:>6} tok -> {samples[-1][1] * 1000:7.1f} ms")
    return samples


async def _one_decode_stream(
    client: httpx.AsyncClient,
    url: str,
    model: str,
    prompt: str,
    input_len: int,
    state: dict[str, int],
    samples: list[tuple[float, float]],
    tokens: int = DECODE_TOKENS,
    dialect: EngineDialect | None = None,
) -> None:
    """Stream one request, recording (resident tokens, inter-token gap).

    `state` is shared across the concurrent streams, so the x axis is the real
    batch (Arrow §3.1). How the stream is held at exactly `tokens` is the dialect's
    extra body keys (vLLM's min_tokens/ignore_eos).
    """
    dialect = dialect or VllmDialect()
    body = {
        "model": model,
        "prompt": prompt,
        "max_tokens": tokens,
        "temperature": 0.0,
        "stream": True,
        **dialect.decode_probe_extras(tokens),
    }
    mine = 0
    last: float | None = None
    state["resident"] += input_len
    try:
        async with client.stream("POST", f"{url}/v1/completions", json=body) as r:
            if r.status_code != 200:
                detail = (await r.aread()).decode("utf-8", "replace")
                raise RuntimeError(
                    f"decode probe failed on {url} ({r.status_code}): {detail[:200]}"
                )
            async for line in r.aiter_lines():
                n = sse_token_count(line)
                if not n:
                    continue
                now = time.monotonic()
                mine += n
                state["resident"] += n
                if last is not None:
                    # The first gap carries the prefill; Arrow §4.3 starts TPOT at t2.
                    samples.append((float(state["resident"]), now - last))
                last = now
    finally:
        state["resident"] -= input_len + mine


async def probe_decode(
    client: httpx.AsyncClient,
    url: str,
    model: str,
    concurrency: tuple[int, ...] = DECODE_CONCURRENCY,
    tokens: int = DECODE_TOKENS,
    dialect: EngineDialect | None = None,
    chars_per_token: float = 3.8,
) -> list[tuple[float, float]]:
    """Concurrent streams per step; samples are (resident batch tokens, gap)."""
    dialect = dialect or VllmDialect()
    prompt, input_len = await make_prompt(client, url, model, 512, dialect, chars_per_token)
    samples: list[tuple[float, float]] = []
    for c in concurrency:
        state = {"resident": 0}
        before = len(samples)
        await asyncio.gather(
            *(
                _one_decode_stream(
                    client, url, model, prompt, input_len, state, samples, tokens, dialect
                )
                for _ in range(c)
            )
        )
        got = samples[before:]
        if got:
            gap = statistics.median(s[1] for s in got)
            batch = statistics.median(s[0] for s in got)
            print(f"    decode  c={c:<3} batch~{batch:>7.0f} tok -> {gap * 1000:6.2f} ms/token")
    return samples


async def profile_instance(
    client: httpx.AsyncClient,
    iid: str,
    url: str,
    model: str,
    sweep: Sweep | None = None,
    dialect: EngineDialect | None = None,
    chars_per_token: float = 3.8,
) -> Profile:
    """Both sweeps against one engine, fitted into its Profile."""
    s = sweep or Sweep()
    dialect = dialect or VllmDialect()
    print(f"  {iid}")
    prefill = await probe_prefill(
        client, url, model, s.prefill_lens, s.prefill_repeats, dialect, chars_per_token
    )
    decode = await probe_decode(
        client, url, model, s.decode_concurrency, s.decode_tokens, dialect, chars_per_token
    )
    a, b, c = fit_quadratic(prefill)
    slope, intercept = fit_linear(decode)
    return Profile(
        iid=iid,
        ttft_a=a,
        ttft_b=b,
        ttft_c=c,
        tpot_slope=slope,
        tpot_intercept=intercept,
    )


@dataclass(frozen=True)
class Sweep:
    """The sweep's shape, per fleet: which prompt lengths, which
    concurrency steps, how many decode tokens per stream. The defaults are the
    module constants; a fleet whose ISL band or batch regime differs overrides
    them from the CLI rather than editing this file."""

    prefill_lens: tuple[int, ...] = PREFILL_LENS
    decode_concurrency: tuple[int, ...] = DECODE_CONCURRENCY
    decode_tokens: int = DECODE_TOKENS
    prefill_repeats: int = PREFILL_REPEATS


async def run(cfg: FleetConfig, only: set[str] | None, sweep: Sweep | None = None) -> int:
    """Profile every selected live instance and write the store."""
    store = ProfileStore(cfg.profiles_path)
    targets = [e for e in cfg.engines if not only or e.iid in only]
    if not targets:
        print("no matching instances", file=sys.stderr)
        return 2

    print(f"profiling {len(targets)} instance(s) against model {cfg.model}")
    dialect = lookup_dialect(cfg.dialect)
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
        for spec in targets:
            r = await client.get(f"{spec.url}{dialect.health_path}", timeout=10.0)
            if r.status_code != 200:
                print(f"  {spec.iid}: not healthy, aborting", file=sys.stderr)
                return 1
            if dialect.tokenize_path is None:
                # The x axis of both fits is then sized off the character
                # ratio - announced, not silent, because a fitted curve reads
                # as measured.
                print(
                    f"  {spec.iid}: the {dialect.name} dialect has no exact-count route; "
                    f"sizing prompts at {cfg.chars_per_token} chars/token, both fits are "
                    "character-estimated"
                )
            profile = await profile_instance(
                client, spec.iid, spec.url, cfg.model, sweep, dialect, cfg.chars_per_token
            )
            store.put(profile)
            print(
                f"    fit: ttft = {profile.ttft_a:.3e}n^2 + {profile.ttft_b:.3e}n "
                f"+ {profile.ttft_c:.4f}"
            )
            print(
                f"         tpot = {profile.tpot_slope:.3e}b + {profile.tpot_intercept:.4f}"
                f"  (max {profile.max_tokens(cfg.slo.tpot_s):.0f} tok at the TPOT SLO)"
            )
    print(f"wrote {len(store)} profile(s) to {cfg.profiles_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI: profile a fleet's instances (all of them, or --only)."""
    ap = argparse.ArgumentParser(description="Profile every engine in a fleet (Arrow §5.2)")
    ap.add_argument("--fleet", required=True, help="fleet config JSON")
    ap.add_argument("--only", action="append", default=[], help="instance id; repeatable")
    ap.add_argument(
        "--prefill-lens",
        default=",".join(str(n) for n in PREFILL_LENS),
        help="comma-separated prompt lengths for the prefill sweep; "
        "cover the fleet's actual ISL band or the fit extrapolates",
    )
    ap.add_argument(
        "--decode-concurrency",
        default=",".join(str(n) for n in DECODE_CONCURRENCY),
        help="comma-separated stream counts for the decode sweep",
    )
    ap.add_argument("--decode-tokens", type=int, default=DECODE_TOKENS)
    ap.add_argument("--prefill-repeats", type=int, default=PREFILL_REPEATS)
    args = ap.parse_args(argv)
    cfg = FleetConfig.load(args.fleet)
    try:
        sweep = Sweep(
            prefill_lens=tuple(int(x) for x in args.prefill_lens.split(",") if x.strip()),
            decode_concurrency=tuple(
                int(x) for x in args.decode_concurrency.split(",") if x.strip()
            ),
            decode_tokens=args.decode_tokens,
            prefill_repeats=args.prefill_repeats,
        )
    except ValueError:
        ap.error("--prefill-lens and --decode-concurrency take comma-separated integers")
    if not sweep.prefill_lens or not sweep.decode_concurrency:
        ap.error("the sweep needs at least one prefill length and one concurrency step")
    if sweep.decode_tokens < 2 or sweep.prefill_repeats < 1:
        ap.error(
            "--decode-tokens needs at least 2 (a gap needs two tokens); "
            "--prefill-repeats at least 1"
        )
    return asyncio.run(run(cfg, set(args.only) or None, sweep))


if __name__ == "__main__":
    raise SystemExit(main())
