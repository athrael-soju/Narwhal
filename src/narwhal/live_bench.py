"""Narwhal Live Bench: drive traffic at a router interactively, and record what was driven.

`narwhal-bench` replays a fixed trace; this console steers a live one.
Commands on stdin adjust the arrival process while it runs - rate, token
bands, named presets, bounded spikes, shared-prefix pools - and every
arrival is appended to a recording that `narwhal-bench --trace-file`
replays verbatim. Improvisation is unrepeatable; the recording is the
experiment: drive a scenario by hand once, then replay it against any
other configuration as a paired run.

Reads commands from stdin, so a file of commands (with `wait`) is a
scripted scenario and a terminal is a live one.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import random
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .bench import Sample, _one

# Named scenarios, in the trace generator's vocabulary: input band, output
# band, and a rate multiplier. The names follow the shapes that events on
# real fleets keep producing: a prefill wall, a decode surge, a global
# spike that raises both loads, a shared-prefix flood, a near-idle valley.
PRESETS: dict[str, dict] = {
    "steady": {"isl": (3000, 6000), "osl": (80, 160), "mult": 1.0},
    "wall": {"isl": (12000, 16000), "osl": (20, 40), "mult": 3.0},
    "squeeze": {"isl": (12000, 16000), "osl": (1, 4), "mult": 3.8},
    "flood": {"isl": (12000, 16000), "osl": (350, 450), "mult": 0.6},
    "spike": {"isl": (12000, 16000), "osl": (300, 400), "mult": 2.0},
    "flip": {"isl": (100, 400), "osl": (500, 800), "mult": 1.5},
    "prefix": {"isl": (8500, 9500), "osl": (40, 80), "mult": 1.0, "prefix": (8000, 8)},
    "valley": {"isl": (1000, 2000), "osl": (50, 100), "mult": 0.05},
}

HELP = f"""\
rate X            base arrivals per second
shape LO-HI LO-HI input and output token bands
mult X            standing rate multiplier
preset NAME       {", ".join(sorted(PRESETS))}
spike Xx Ns       temporary extra multiplier, then auto-revert
prefix on LEN [POOL]   shared heads of LEN tokens over POOL prefixes (default 8)
prefix off
status            rolling window and the router's own view
wait N            do nothing for N seconds (for scripted sessions)
quit              stop driving and print the session score\
"""


@dataclass
class Scenario:
    """The arrival process's current settings; commands mutate it in place."""

    rate: float = 1.0
    isl: tuple[int, int] = (3000, 6000)
    osl: tuple[int, int] = (80, 160)
    mult: float = 1.0
    spike_mult: float = 1.0
    spike_until: float = 0.0
    prefix: tuple[int, int] | None = None  # (head_len_tokens, pool_size)

    def effective_rate(self, now: float) -> float:
        """Arrivals per second right now: base rate, multiplier, live spike."""
        spike = self.spike_mult if now < self.spike_until else 1.0
        return self.rate * self.mult * spike


def _band(text: str) -> tuple[int, int]:
    lo, _, hi = text.partition("-")
    pair = (int(lo), int(hi or lo))
    if pair[0] <= 0 or pair[1] < pair[0]:
        raise ValueError(f"{text!r} is not LO-HI with 0 < LO <= HI")
    return pair


def apply_command(scn: Scenario, line: str, now: float) -> str:
    """Mutate the scenario per one command line; the return is echoed.

    `wait`, `status`, `quit` and `help` belong to the console loop, not
    the scenario; passing them here raises like any unknown command.
    """
    words = line.split()
    match words:
        case ["rate", x]:
            scn.rate = float(x)
            return f"rate {scn.rate:g}/s"
        case ["shape", isl, osl]:
            scn.isl, scn.osl = _band(isl), _band(osl)
            return f"shape {scn.isl[0]}-{scn.isl[1]} in, {scn.osl[0]}-{scn.osl[1]} out"
        case ["mult", x]:
            scn.mult = float(x)
            return f"mult {scn.mult:g}x"
        case ["preset", name] if name in PRESETS:
            p = PRESETS[name]
            scn.isl, scn.osl, scn.mult = p["isl"], p["osl"], p["mult"]
            head, pool = p.get("prefix", (0, 0))
            scn.prefix = (head, pool) if head else None
            return f"preset {name}: {scn.isl} in, {scn.osl} out, {scn.mult:g}x" + (
                f", {pool} shared {head}-token heads" if head else ""
            )
        case ["spike", m, d] if m.endswith("x") and d.endswith("s"):
            scn.spike_mult = float(m[:-1])
            scn.spike_until = now + float(d[:-1])
            return f"spike {scn.spike_mult:g}x for {float(d[:-1]):g}s"
        case ["prefix", "on", head, *pool]:
            scn.prefix = (int(head), int(pool[0]) if pool else 8)
            return f"prefix on: {scn.prefix[1]} heads of {scn.prefix[0]} tokens"
        case ["prefix", "off"]:
            scn.prefix = None
            return "prefix off"
        case _:
            raise ValueError(f"unknown command {line!r} (try `help`)")


@dataclass
class Session:
    """Everything one driving session accumulates."""

    samples: list[tuple[float, Sample]] = field(default_factory=list)
    sent: int = 0
    started: float = field(default_factory=time.monotonic)

    def window(self, seconds: float, ttft_slo: float, tpot_slo: float) -> str:
        """One line scoring the trailing window by the bench's metric."""
        cut = time.monotonic() - seconds
        recent = [s for at, s in self.samples if at >= cut]
        met = sum(
            1
            for s in recent
            if s.error is None
            and s.output_len >= s.wanted_len
            and s.ttft_s is not None
            and s.ttft_s <= ttft_slo
            and (s.tpot_s is None or s.tpot_s <= tpot_slo)
        )
        errs = sum(1 for s in recent if s.error is not None)
        frac = f"{met / len(recent):.0%}" if recent else "-"
        return f"last {seconds:.0f}s: {met}/{len(recent)} met ({frac}), {errs} errors"


async def _router_line(client: httpx.AsyncClient, base: str) -> str:
    try:
        s = (await client.get(f"{base}/arrow/state", timeout=4.0)).json()
    except (httpx.HTTPError, ValueError):
        return "router: unreachable"
    pools = s.get("pools", {})
    load = s.get("load", {})
    poa = s.get("poa", {})
    flips = s.get("flips")
    return (
        f"router: {len(pools.get('prefill', []))}P{len(pools.get('decode', []))}D"
        f" | Lp={load.get('prefill', 0):.2f} Ld={load.get('decode', 0):.2f}"
        f" | {poa.get('regime', '?')} regret={poa.get('regret')}"
        f" | flips={len(flips) if isinstance(flips, list) else flips}"
        f" | failed={s.get('failed')} unserved={s.get('unserved')}"
    )


async def run(args: argparse.Namespace) -> int:
    """The session: an arrival loop, a command reader, a heartbeat."""
    scn = Scenario()
    ses = Session()
    rng = random.Random(args.seed)  # noqa: S311 - load synthesis, not cryptography
    record = Path(args.record)
    record.parent.mkdir(parents=True, exist_ok=True)
    rec = record.open("w")
    stop = asyncio.Event()
    limits = httpx.Limits(max_connections=1024, max_keepalive_connections=512)

    async with httpx.AsyncClient(timeout=httpx.Timeout(900.0, connect=10.0), limits=limits) as c:

        async def arrivals() -> None:
            pending: set[asyncio.Task] = set()
            while not stop.is_set():
                r = scn.effective_rate(time.monotonic())
                if r <= 0:
                    await asyncio.sleep(0.25)
                    continue
                await asyncio.sleep(rng.expovariate(r))
                if stop.is_set():
                    break
                isl = rng.randint(*scn.isl)
                osl = rng.randint(*scn.osl)
                prefix = None
                if scn.prefix:
                    head, pool = scn.prefix
                    prefix = (rng.randrange(pool) + 1, head)
                row: dict = {
                    "at": round(time.monotonic() - ses.started, 4),
                    "input_len": isl,
                    "output_len": osl,
                }
                if prefix:
                    row["prefix_id"], row["prefix_len"] = prefix
                rec.write(json.dumps(row) + "\n")
                rec.flush()
                ses.sent += 1
                idx = ses.sent

                async def fire(
                    idx: int, isl: int, osl: int, prefix: tuple[int, int] | None
                ) -> None:
                    s = await _one(c, args.base, args.model, idx, isl, osl, prefix)
                    ses.samples.append((time.monotonic(), s))

                t = asyncio.create_task(fire(idx, isl, osl, prefix))
                pending.add(t)
                t.add_done_callback(pending.discard)
            if pending:
                print(f"draining {len(pending)} in flight...")
                await asyncio.gather(*pending, return_exceptions=True)

        async def heartbeat() -> None:
            while not stop.is_set():
                await asyncio.sleep(args.heartbeat)
                if stop.is_set() or not args.heartbeat:
                    break
                now = time.monotonic()
                print(
                    f"[{now - ses.started:6.0f}s] {scn.effective_rate(now):g}/s"
                    f" | sent {ses.sent} | {ses.window(60, args.ttft_slo, args.tpot_slo)}"
                )

        async def console() -> None:
            loop = asyncio.get_running_loop()
            while not stop.is_set():
                line = await loop.run_in_executor(None, sys.stdin.readline)
                if not line:  # EOF: a scripted session ended
                    stop.set()
                    return
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                words = line.split()
                try:
                    match words:
                        case ["quit"] | ["stop"] | ["exit"]:
                            stop.set()
                        case ["help"]:
                            print(HELP)
                        case ["status"]:
                            print(ses.window(60, args.ttft_slo, args.tpot_slo))
                            print(await _router_line(c, args.base))
                        case ["wait", n]:
                            await asyncio.sleep(float(n))
                        case _:
                            print(apply_command(scn, line, time.monotonic()))
                except ValueError as exc:
                    print(exc)

        tasks = [asyncio.create_task(t()) for t in (arrivals, console, heartbeat)]
        await stop.wait()
        for t in tasks[1:]:
            t.cancel()
        await tasks[0]  # drain in-flight requests
        for t in tasks[1:]:
            with contextlib.suppress(asyncio.CancelledError):
                await t

    rec.close()
    done = [s for _, s in ses.samples]
    met = sum(
        1
        for s in done
        if s.error is None
        and s.output_len >= s.wanted_len
        and s.ttft_s is not None
        and s.ttft_s <= args.ttft_slo
        and (s.tpot_s is None or s.tpot_s <= args.tpot_slo)
    )
    errs = sum(1 for s in done if s.error is not None)
    frac = f" ({met / len(done):.1%})" if done else ""
    print(f"session: {ses.sent} sent, {met}/{len(done)} met{frac}, {errs} errors")
    print(f"recorded {record} - replay: narwhal-bench --trace-file {record} --rates 1.0")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI: connect to a router and hand the arrival process to stdin."""
    ap = argparse.ArgumentParser(
        description="Narwhal Live Bench: drive traffic at a router interactively"
    )
    ap.add_argument("--base", default="http://127.0.0.1:8000", help="router base URL")
    ap.add_argument("--model", required=True)
    ap.add_argument("--ttft-slo", type=float, required=True)
    ap.add_argument("--tpot-slo", type=float, required=True)
    ap.add_argument(
        "--record",
        default=f"runs/local/live-bench-{int(time.time())}.jsonl",
        help="append every arrival here as a replayable trace",
    )
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument(
        "--heartbeat",
        type=float,
        default=20.0,
        help="seconds between status lines; 0 silences them",
    )
    args = ap.parse_args(argv)
    print(f"driving {args.base}; `help` lists commands, `quit` ends the session")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
