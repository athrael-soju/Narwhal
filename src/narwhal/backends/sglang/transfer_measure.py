"""Measure SGLang KV handoff payloads between an idle prefill and decode engine.

Engine hosts run a copy of this file, checked by its SHA-256, with the host's own Python, so it
imports only the standard library. The transfer record it writes sizes a fabric budget through
`fabric_budget.py calculate --transfer-record`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import Request, urlopen

SCHEMA = "narwhal.kv-transfer-measurement"
SERIES = "sglang:kv_transfer_total_mb"
TRANSFER_SERIES = re.compile(
    rf"^{re.escape(SERIES)}_(sum|count)(\{{[^}}]*\}})?\s+([0-9.eE+-]+)\s*$", re.MULTILINE
)

Call = Callable[..., bytes]


def positive_integer(value: int, name: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def transfer_series(metrics: str) -> dict[str, dict[str, float]]:
    series: dict[str, dict[str, float]] = {}
    for kind, labels, value in TRANSFER_SERIES.findall(metrics):
        series.setdefault(labels, {})[kind] = float(value)
    return series


def transfer_payload(before: str, after: str) -> tuple[int, int]:
    """Return the bytes per handoff summed over TP ranks, and the handoff count."""
    start, end = transfer_series(before), transfer_series(after)
    if not end:
        raise ValueError(f"the metrics hold no {SERIES} series")
    payload = 0.0
    handoffs = set()
    for labels, totals in end.items():
        previous = start.get(labels, {"sum": 0.0, "count": 0.0})
        count = totals.get("count", 0.0) - previous.get("count", 0.0)
        mib = totals.get("sum", 0.0) - previous.get("sum", 0.0)
        if count < 1 or mib <= 0:
            raise ValueError(f"no KV transfer recorded between the snapshots for {labels or '{}'}")
        handoffs.add(count)
        payload += mib / count
    if len(handoffs) != 1:
        raise ValueError("TP ranks recorded different handoff counts between the snapshots")
    return math.ceil(payload * 1024 * 1024), int(handoffs.pop())


def measure(args: argparse.Namespace) -> dict:
    """Put the decode engine in the decode role, send handoffs and restore its role."""
    positive_integer(args.prompt_tokens, "prompt_tokens")
    positive_integer(args.handoffs, "handoffs")
    headers = {"Content-Type": "application/json"}
    if os.environ.get(args.api_key_env):
        headers["Authorization"] = "Bearer " + os.environ[args.api_key_env]

    def call(url: str, body: dict | None = None) -> bytes:
        data = json.dumps(body).encode() if body is not None else None
        with urlopen(Request(url, data=data, headers=headers), timeout=120) as response:
            content: bytes = response.read()
            return content

    def scheduler(base: str) -> dict:
        info = json.loads(call(base.rstrip("/") + "/server_info"))
        state: dict = (info.get("internal_states") or [{}])[0]
        return state

    def switch(base: str, role: str, memory: float | None = None) -> None:
        body: dict = {"new_role": role}
        if memory is not None:
            body["decode_cuda_graph_memory_gb"] = memory
        result = json.loads(call(base.rstrip("/") + "/pd_role_switch", body))
        if result.get("success") is not True:
            raise ValueError(f"{base} refused the switch to {role}: {result.get('message')}")

    if scheduler(args.prefill).get("disaggregation_mode") != "prefill":
        raise ValueError("--prefill must select an engine serving the prefill role")
    state = scheduler(args.decode)
    found = state.get("disaggregation_mode")
    memory = state.get("decode_cuda_graph_memory_gb") or json.loads(args.launch_config.read_text())[
        "runtime"
    ].get("decode_cuda_graph_memory_gb")
    if found == "decode":
        switch(args.decode, "prefill")
    switch(args.decode, "decode", float(memory) if memory else None)
    try:
        return send_handoffs(args, call)
    finally:
        if found == "prefill":
            switch(args.decode, "prefill")


def send_handoffs(args: argparse.Namespace, call: Call) -> dict:
    """Send paired cold legs, then write the metric snapshots and the transfer record."""
    metrics_url = args.prefill.rstrip("/") + "/metrics"
    args.out_dir.mkdir(mode=0o700)
    before = call(metrics_url)
    draw = random.SystemRandom()
    for _ in range(args.handoffs):
        body = {
            "model": args.model,
            "prompt": [draw.randrange(100, 10000) for _ in range(args.prompt_tokens)],
            "max_tokens": 1,
            "temperature": 0,
            "bootstrap_host": args.bootstrap_host,
            "bootstrap_port": args.bootstrap_port,
            "bootstrap_room": draw.getrandbits(63),
        }
        with ThreadPoolExecutor(2) as legs:
            decode = legs.submit(call, args.decode.rstrip("/") + "/v1/completions", body)
            call(args.prefill.rstrip("/") + "/v1/completions", body)
            decode.result()
    after = call(metrics_url)
    for name, data in (("before.prom", before), ("after.prom", after)):
        with (args.out_dir / name).open("xb") as stream:
            stream.write(data)
    payload, handoffs = transfer_payload(before.decode(), after.decode())
    record = {
        "schema": SCHEMA,
        "schema_version": 1,
        "series": SERIES,
        # The packaged launch record of the prefill engine, which binds the record to its role.
        "launch_config_sha256": hashlib.sha256(args.launch_config.read_bytes()).hexdigest(),
        "prompt_tokens": args.prompt_tokens,
        "payload_bytes_per_handoff": payload,
        "handoffs": handoffs,
        "metrics_sha256": {
            "before.prom": hashlib.sha256(before).hexdigest(),
            "after.prom": hashlib.sha256(after).hexdigest(),
        },
    }
    fd = os.open(args.out_dir / "transfer.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(record, stream, indent=2)
        stream.write("\n")
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure SGLang KV handoff payloads and write a transfer record."
    )
    parser.add_argument("--prefill", required=True, help="prefill engine base URL")
    parser.add_argument("--decode", required=True, help="decode engine base URL")
    parser.add_argument("--bootstrap-host", required=True)
    parser.add_argument("--bootstrap-port", type=int, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--launch-config",
        type=Path,
        required=True,
        help="engine launch record of the decode engine",
    )
    parser.add_argument("--prompt-tokens", type=int, required=True)
    parser.add_argument("--handoffs", type=int, default=8)
    parser.add_argument("--api-key-env", default="NARWHAL_ENGINE_API_KEY")
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        record = measure(args)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        if isinstance(error, FileExistsError):
            parser.exit(2, "Output exists; select a fresh --out-dir.\n")
        if isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError):
            parser.exit(2, f"{error}\n")
        parser.exit(2, "Check the engine URLs, the launch record and the output directory.\n")
    print(
        json.dumps(
            {
                "transfer_record": str(args.out_dir / "transfer.json"),
                "payload_bytes_per_handoff": record["payload_bytes_per_handoff"],
                "handoffs": record["handoffs"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
