"""Load client process that offers one client's share of a rate."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx
import uvloop

from tools.measurement import load_trial as trial

from .files import open_private
from .fleet import MODEL


def text_prompt(ids: list[int]) -> str:
    """Return one letter per token ID."""
    return "".join(chr(ord("a") + i) for i in ids)


def client_bodies(
    input_tokens: int, output_tokens: int, requests: int, clients: int, index: int
) -> list[tuple[int, dict]]:
    """Return the sequences and request bodies offered by one client."""
    workload = {
        "schema": 1,
        "kind": "synthetic-token-length",
        "model": MODEL,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "seed": 1729,
        "token_pool": list(range(26)),
    }
    offers = []
    for sequence in range(index, requests, clients):
        body = trial.body_for(workload, sequence)
        body["prompt"] = text_prompt(body["prompt"])
        offers.append((sequence, body))
    return offers


async def sleep_until(moment: float) -> None:
    await asyncio.sleep(max(0.0, moment - time.monotonic()))


async def offer_requests(args: argparse.Namespace, bodies: list[tuple[int, dict]]) -> int:
    async with httpx.AsyncClient(
        trust_env=False, timeout=args.timeout, limits=httpx.Limits(max_connections=None)
    ) as client:
        with open_private(args.out) as output:
            print(json.dumps({"ready": True}), flush=True)
            start_at = json.loads(sys.stdin.readline())["start_at"]

            async def send(sequence: int, body: dict) -> None:
                scheduled = start_at + sequence / args.rate
                await sleep_until(scheduled)
                row = await trial.request_one(
                    client,
                    args.base,
                    body,
                    f"r{args.rate:g}-{sequence}",
                    scheduled,
                    args.timeout,
                )
                row["sequence"] = sequence
                output.write(json.dumps(row, allow_nan=False) + "\n")
                output.flush()

            await asyncio.gather(*(send(sequence, body) for sequence, body in bodies))
    return 0


def run_client(args: argparse.Namespace) -> int:
    """Offer one client's share of a rate at the start time read from stdin."""
    bodies = client_bodies(
        args.input_tokens, args.output_tokens, args.requests, args.clients, args.index
    )
    return uvloop.run(offer_requests(args, bodies))


def client_argv(
    args: argparse.Namespace, base: str, rate: float, requests: int, index: int, directory: Path
) -> list[str]:
    """Command line of load client `index` for one offered rate against `base`."""
    return [
        args.python,
        "-m",
        "tools.measurement.router_benchmark.cli",
        "client",
        "--base",
        base,
        "--input-tokens",
        str(args.input_tokens),
        "--output-tokens",
        str(args.output_tokens),
        "--rate",
        repr(rate),
        "--requests",
        str(requests),
        "--clients",
        str(args.clients),
        "--index",
        str(index),
        "--timeout",
        repr(args.timeout),
        "--out",
        str(directory / f"client-{index}.jsonl"),
    ]
