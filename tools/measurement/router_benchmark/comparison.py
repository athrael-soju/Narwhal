"""Alternating base and branch runs and their comparison document."""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys

from tools.measurement import load_trial as trial

from .cpus import Allocation
from .files import open_private
from .render import comparison_text
from .report import VERSION
from .run import Run

COMPARISON_KIND = "narwhal-router-benchmark-comparison"


def comparison(runs: list[dict], base_label: str, branch_label: str) -> dict:
    """Summarize alternating runs per version as medians and the branch/base ratio."""
    versions = {}
    for label in (base_label, branch_label):
        mine = [run for run in runs if run["label"] == label]
        points = [run["point"] for run in mine]
        complete = all(point is not None for point in points)
        rps = [point and point["offered_rps"] for point in points]
        frames = [point and point["relayed_frames_per_router_cpu_s"] for point in points]
        versions[label] = {
            "router_src": mine[0]["router_src"],
            "router_source": mine[0]["router_source"],
            "point_rps": rps,
            "relayed_frames_per_router_cpu_s": frames,
            "median_point_rps": statistics.median(rps) if complete else None,
            "median_relayed_frames_per_router_cpu_s": (
                statistics.median(frames) if complete else None
            ),
        }
    base = versions[base_label]["median_relayed_frames_per_router_cpu_s"]
    branch = versions[branch_label]["median_relayed_frames_per_router_cpu_s"]
    return {
        "kind": COMPARISON_KIND,
        "version": VERSION,
        "order": [run["label"] for run in runs],
        "runs": [
            {key: run[key] for key in ("label", "out", "router_source", "point")} for run in runs
        ],
        "versions": versions,
        "ratio": branch / base if branch is not None and base else None,
    }


def compare(args: argparse.Namespace, allocation: Allocation) -> int:
    """Alternate base and branch runs for `--runs` rounds and write the comparison."""
    args.out.mkdir(mode=0o700, parents=True)
    sources = {args.base_label: args.base_src, args.branch_label: args.branch_src}
    runs = []
    for round_index in range(args.runs):
        for offset, label in enumerate(sources):
            directory = args.out / f"{2 * round_index + offset + 1}-{label}"
            code = asyncio.run(Run(args, sources[label], label, directory, allocation).execute())
            if code != 0:
                print(f"Comparison stopped: {directory} exited {code}.", file=sys.stderr)
                return code
            report = json.loads((directory / "report.json").read_text())
            runs.append(
                {
                    "label": label,
                    "out": str(directory),
                    "router_src": report["router_src"],
                    "router_source": report["router_source"],
                    "point": report["point"],
                }
            )
    doc = comparison(runs, args.base_label, args.branch_label)
    trial.private_json(args.out / "comparison.json", doc)
    text = comparison_text(doc)
    with open_private(args.out / "comparison.txt") as out:
        out.write(text)
    print(text, end="", flush=True)
    return 0
