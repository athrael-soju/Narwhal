"""Measure router relay throughput per router CPU-second against simulated engines."""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import re
import resource
import subprocess
import sys
from dataclasses import fields
from itertools import pairwise
from pathlib import Path

import httpx

from .processes import ROOT

sys.path.insert(0, str(ROOT / "src"))
from narwhal.config import FleetConfig

from .client import run_client
from .comparison import compare
from .cpus import allocate, parse_cpu_list, thread_siblings
from .run import Run

FLEET_DEFAULTS = {item.name: item.default for item in fields(FleetConfig)}
LABEL = re.compile(r"[A-Za-z0-9._-]+")


def rate_list(text: str) -> list[float]:
    return [float(value) for value in text.split(",")]


def parser_for() -> argparse.ArgumentParser:
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--out", required=True, type=Path, help="fresh private output directory")
    shared.add_argument("--python", default=sys.executable, help="interpreter for every child")
    shared.add_argument("--cpus", required=True, type=parse_cpu_list, help="CPU list, router first")
    shared.add_argument("--engines", type=int, default=8)
    shared.add_argument("--prefill-engines", type=int, default=2)
    shared.add_argument("--input-tokens", type=int, default=512)
    shared.add_argument("--output-tokens", type=int, default=256)
    shared.add_argument("--frames-per-write", type=int, default=1)
    shared.add_argument("--token-interval", type=float, default=0.02)
    shared.add_argument("--prefill-seconds", type=float, default=0.005)
    shared.add_argument("--rates", required=True, type=rate_list, help="ascending offered rps")
    shared.add_argument("--duration", type=float, default=60.0)
    shared.add_argument("--warmup", type=float, default=10.0)
    shared.add_argument("--clients", type=int, default=8)
    shared.add_argument("--ttft-slo", type=float, default=2.0)
    shared.add_argument("--tpot-slo", type=float, default=0.1)
    shared.add_argument("--timeout", type=float, default=120.0)
    shared.add_argument("--drain-timeout", type=float, default=300.0)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", parents=[shared], help="sweep one router source")
    run.add_argument("--router-src", required=True, type=Path)
    run.add_argument("--label", default="router")
    compare = commands.add_parser("compare", parents=[shared], help="alternate two sources")
    compare.add_argument("--base-src", required=True, type=Path)
    compare.add_argument("--branch-src", required=True, type=Path)
    compare.add_argument("--base-label", default="base")
    compare.add_argument("--branch-label", default="branch")
    compare.add_argument("--runs", type=int, default=3)
    offer = commands.add_parser("client", help="one load client process")
    offer.add_argument("--base", required=True)
    offer.add_argument("--input-tokens", type=int, required=True)
    offer.add_argument("--output-tokens", type=int, required=True)
    offer.add_argument("--rate", type=float, required=True)
    offer.add_argument("--requests", type=int, required=True)
    offer.add_argument("--clients", type=int, required=True)
    offer.add_argument("--index", type=int, required=True)
    offer.add_argument("--timeout", type=float, required=True)
    offer.add_argument("--out", type=Path, required=True)
    return parser


def check(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Reject invalid run or compare options with exit status 2."""
    rates = args.rates
    if any(not math.isfinite(rate) or rate <= 0 for rate in rates) or any(
        later <= earlier for earlier, later in pairwise(rates)
    ):
        parser.error("--rates must be ascending, positive and finite")
    for name in (
        "token_interval",
        "duration",
        "warmup",
        "ttft_slo",
        "tpot_slo",
        "timeout",
        "drain_timeout",
    ):
        value = getattr(args, name)
        if not math.isfinite(value) or value <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive and finite")
    if not math.isfinite(args.prefill_seconds) or args.prefill_seconds < 0:
        parser.error("--prefill-seconds must be nonnegative and finite")
    for name in ("input_tokens", "output_tokens", "frames_per_write", "clients"):
        if getattr(args, name) < 1:
            parser.error(f"--{name.replace('_', '-')} must be at least 1")
    if not 1 <= args.prefill_engines < args.engines:
        parser.error("--prefill-engines must be at least 1 and below --engines")
    correction = FLEET_DEFAULTS["reactive_decode_correction_max"]
    if args.tpot_slo <= correction * args.token_interval:
        parser.error(f"--tpot-slo must exceed {correction:g} x --token-interval")
    first_token = FLEET_DEFAULTS["first_token_timeout_s"]
    if args.frames_per_write * args.token_interval >= first_token:
        parser.error(
            f"--frames-per-write x --token-interval must stay below the {first_token:g} s "
            "first-token timeout"
        )
    if args.duration <= args.output_tokens * args.token_interval:
        parser.error("--duration must exceed --output-tokens x --token-interval")
    if round(rates[0] * args.duration) < 1:
        parser.error("--rates and --duration must offer at least one request per rate")
    if args.out.exists():
        parser.error("--out must name a new directory")
    labels = [args.label] if args.command == "run" else [args.base_label, args.branch_label]
    if any(LABEL.fullmatch(label) is None for label in labels) or len(set(labels)) != len(labels):
        parser.error("labels must be distinct and use letters, digits, '.', '_' or '-'")
    if args.command == "compare" and args.runs < 1:
        parser.error("--runs must be at least 1")


def main(argv: list[str] | None = None) -> int:
    parser = parser_for()
    args = parser.parse_args(argv)
    if args.command == "client":
        return run_client(args)
    check(parser, args)
    args.out = args.out.resolve()
    try:
        allocation = allocate(args.cpus, thread_siblings(args.cpus[0]), args.clients, args.engines)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    try:
        os.sched_setaffinity(0, {allocation.driver})
        _, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
        if args.command == "compare":
            return compare(args, allocation)
        return asyncio.run(Run(args, args.router_src, args.label, args.out, allocation).execute())
    except KeyboardInterrupt:
        print(f"Router benchmark interrupted; retain {args.out}.", file=sys.stderr)
        return 130
    except (
        OSError,
        ValueError,
        KeyError,
        RuntimeError,
        subprocess.SubprocessError,
        httpx.HTTPError,
    ) as error:
        detail = str(error) or type(error).__name__
        print(f"Router benchmark blocked: {detail}. Retain {args.out}.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
