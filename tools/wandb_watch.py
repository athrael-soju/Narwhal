#!/usr/bin/env python3
"""Stream a router's /arrow/state to Weights & Biases.

Read-only: polls the state endpoint and logs the topology, both pool loads,
throughput counters and every flip, so a running comparison has a live
dashboard. Survives router restarts between arms: counters re-base when
`served` falls, and the current arm/cell is read from the active bench
process's --out path so the chart is annotated with what is driving load.

    WANDB_API_KEY=... wandb_watch.py --base http://localhost:8011 \
        --project narwhal --run-name stress-ladder --interval 5

A watcher that cannot authenticate must not die into its own log file and
read as "nothing to report" from the operator's side. It refuses to start:
no credential (WANDB_API_KEY, an api.wandb.ai entry in ~/.netrc, or an
offline WANDB_MODE) is a nonzero exit on stderr, and a started watcher prints
its run URL on the first line so a harness can assert on it before walking
away. `--once` exercises exactly that path - credential, run, one poll - and
exits 0, for smoke checks from scripts.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


def credentials_source(env: dict[str, str]) -> str | None:
    """Where the W&B credential comes from, or None when the watcher would
    start unauthenticated - which is the failure that ran silent."""
    if env.get("WANDB_API_KEY"):
        return "WANDB_API_KEY"
    mode = env.get("WANDB_MODE", "").lower()
    if mode in ("offline", "disabled"):
        return f"WANDB_MODE={mode}"
    netrc = Path(env["NETRC"]) if env.get("NETRC") else Path(env.get("HOME", "")) / ".netrc"
    try:
        if "api.wandb.ai" in netrc.read_text():
            return str(netrc)
    except OSError:
        pass
    return None


def state(base: str) -> dict | None:
    try:
        with urllib.request.urlopen(f"{base}/arrow/state", timeout=5) as r:
            return json.load(r)
    except Exception:
        return None


def active_cell() -> str:
    """`arm.tag` from the running bench's --out path, or "idle"."""
    try:
        out = subprocess.run(
            ["pgrep", "-af", "narwhal-bench"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout
    except Exception:
        return "unknown"
    m = re.search(r"--out \S*/(\w+)\.([\w.]+)\.samples", out)
    return f"{m.group(1)}.{m.group(2)}" if m else "idle"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8011")
    ap.add_argument("--project", default="narwhal")
    ap.add_argument("--run-name", default="fleet-watch")
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument(
        "--once",
        action="store_true",
        help="verify the credential, start the run, poll once, exit - a harness smoke check",
    )
    args = ap.parse_args(argv)

    source = credentials_source(os.environ)
    if source is None:
        print(
            "wandb_watch: no W&B credential - set WANDB_API_KEY, or put an api.wandb.ai"
            " entry in ~/.netrc; refusing to watch silently",
            file=sys.stderr,
        )
        return 2

    try:
        import wandb  # after the credential check: a missing package is not the first error
    except ImportError:
        print(
            "wandb_watch: wandb is not installed - pip install narwhal-inference[wandb]",
            file=sys.stderr,
        )
        return 2

    try:
        run = wandb.init(project=args.project, name=args.run_name, resume="allow")
    except Exception as exc:
        print(f"wandb_watch: wandb.init failed ({source}): {exc}", file=sys.stderr)
        return 2
    print(f"wandb_watch: run {getattr(run, 'url', None) or '(no url)'}", flush=True)

    served_base = failed_base = 0
    served_prev = failed_prev = flips_prev = 0
    total_flips = 0
    step = 0

    while True:
        s = state(args.base)
        if s is None:
            if args.once:
                print(
                    f"wandb_watch: {args.base} not reachable; the poll loop keeps retrying",
                    flush=True,
                )
                return 0
            time.sleep(args.interval)
            continue
        if s["served"] < served_prev:  # router restarted: new arm or cell
            served_base += served_prev
            failed_base += failed_prev
            flips_prev = 0
        served_prev, failed_prev = s["served"], s["failed"]
        new_flips = len(s["flips"]) - flips_prev
        if new_flips > 0:
            for f in s["flips"][-new_flips:]:
                total_flips += 1
                run.log(
                    {
                        "flip/to_prefill": 1 if f["to"] == "prefill" else 0,
                        "flip/to_decode": 1 if f["to"] == "decode" else 0,
                        "flip/by_algorithm1": 1 if f["by"] == "algorithm1" else 0,
                        "flip/carrying": f["prefill_inflight"] + f["decode_inflight"],
                    },
                    step=step,
                )
        flips_prev = len(s["flips"])

        cell = active_cell()
        run.log(
            {
                "pool/prefill": len(s["pools"]["prefill"]),
                "pool/decode": len(s["pools"]["decode"]),
                "load/prefill": s["load"]["prefill"],
                "load/decode": s["load"]["decode"],
                "served_total": served_base + s["served"],
                "failed_total": failed_base + s["failed"],
                "unserved": s["unserved"],
                "flips_total": total_flips,
                "refusals": len(s["flips_refused"]),
                "resident": sum(v["prefill"] + v["decode"] for v in s["resident"].values()),
                "cell": cell,
            },
            step=step,
        )
        step += 1
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
