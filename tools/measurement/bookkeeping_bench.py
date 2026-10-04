"""Time per-request router bookkeeping against synthetic router state."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from narwhal.config import FleetConfig
from narwhal.observability.journal import RunJournal
from narwhal.scheduling.scheduler.occupancy import decode_admits
from narwhal.serving.router.routing import NarwhalRouter
from narwhal.types import Phase, Request
from tools.measurement.router_benchmark.fleet import Shape, write_fleet

SHAPE = Shape(
    engines=8,
    prefill_engines=2,
    input_tokens=512,
    output_tokens=256,
    token_interval_s=0.02,
    frames_per_write=1,
    prefill_s=0.005,
    ttft_slo_s=2.0,
    tpot_slo_s=0.1,
)


def router(out: Path, engines: int, prefill_engines: int) -> NarwhalRouter:
    """A router over `engines` simulated engine records, with profiles and no network."""
    out.mkdir(parents=True)
    lines = [
        {
            "iid": f"e{index}",
            "url": f"http://e{index}",
            "version": "simulated",
            "process_start_time_seconds": 100.0,
        }
        for index in range(engines)
    ]
    shape = Shape(**{**SHAPE.__dict__, "engines": engines, "prefill_engines": prefill_engines})
    cfg = FleetConfig.load(write_fleet(out, lines, shape))
    journal = RunJournal(out / "journal.jsonl")
    return NarwhalRouter(cfg, journal)


def resident(narwhal: NarwhalRouter, count: int, *, tag: str) -> list[tuple[str, str]]:
    """Spread `count` decode residents over the decode engines; return (iid, rid) pairs."""
    decode = [i for i in narwhal.monitor.instances.values() if i.role.value == "decode"]
    placed = []
    for index in range(count):
        inst = decode[index % len(decode)]
        request = Request(f"{tag}{index}", 512, phase=Phase.DECODE, output_len=index % 256)
        narwhal.monitor.dispatched(inst.iid, request)
        placed.append((inst.iid, request.rid))
    return placed


def waiting(narwhal: NarwhalRouter, count: int) -> None:
    for index in range(count):
        narwhal.monitor.waiting[f"w{index}"] = Request(f"w{index}", 512, arrived_at=0.0)


def per_call_us(call: Callable[[], object], *, calls: int, rounds: int) -> float:
    """Median over `rounds` of the mean microseconds per call."""
    samples = []
    for _ in range(rounds):
        started = time.perf_counter()
        for _ in range(calls):
            call()
        samples.append((time.perf_counter() - started) / calls * 1e6)
    return statistics.median(samples)


def measure(out: Path, calls: int, rounds: int) -> list[dict]:
    """Time each scoped hot path at the sizes #255 compares."""
    rows = []

    def row(item: str, size: str, value: float) -> None:
        rows.append({"item": item, "size": size, "per_call_us": round(value, 3)})

    for residents in (10, 50):
        narwhal = router(out / f"token-{residents}", 2, 1)
        placed = resident(narwhal, residents, tag="t")
        iid, rid = placed[0]
        narwhal.monitor.output_token(iid, rid)
        value = per_call_us(
            partial(narwhal.monitor.output_token, iid, rid), calls=calls, rounds=rounds
        )
        row("output_token", f"{residents} resident decode requests on the engine", value)

    for engines, residents in ((32, 30), (32, 800)):
        narwhal = router(out / f"admit-{engines}-{residents}", engines, engines // 4)
        resident(narwhal, residents, tag="d")
        request = Request("new", 512, output_len=0, wanted_len=256)
        admit = partial(decode_admits, narwhal.scheduler, request, ready_s=0.1, ttft_s=0.5)
        value = per_call_us(admit, calls=max(1, calls // 50), rounds=rounds)
        row("decode_admits", f"{engines} engines, {residents} resident decode requests", value)

    for count in (5, 50):
        narwhal = router(out / f"project-{count}", 8, 2)
        waiting(narwhal, count)
        request = Request("p", 512, arrived_at=0.0)
        scorer = narwhal.controller.scorer
        value = per_call_us(
            partial(scorer.project_prefill, 1.0, request), calls=max(1, calls // 50), rounds=rounds
        )
        row("project_prefill", f"{count} waiting requests", value)

    for engines in (8, 32):
        narwhal = router(out / f"place-{engines}", engines, engines // 4)
        request = Request("q", 512)
        value = per_call_us(
            partial(narwhal.scheduler.schedule, request), calls=max(1, calls // 50), rounds=rounds
        )
        row("schedule (prefill)", f"{engines} engines", value)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calls", type=int, default=20000, help="output_token calls per round")
    parser.add_argument("--rounds", type=int, default=7)
    args = parser.parse_args(argv)
    if args.calls < 50 or args.rounds < 1:
        parser.error("--calls must be at least 50 and --rounds at least 1")
    with tempfile.TemporaryDirectory() as directory:
        rows = measure(Path(directory), args.calls, args.rounds)
    for row in rows:
        print(json.dumps(row))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
