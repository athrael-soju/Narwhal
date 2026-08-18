#!/usr/bin/env python3
"""Score one walk arm phase by phase.

The bench's client rows carry no arrival times, but `rid` is the trace index
and the trace is deterministic: regenerating it with the same seed and
segment spec recovers each request's arrival second, and with it the phase.
Per phase: offered, attainment under the same rules as `bench.score`, the
miss kinds, and TTFT percentiles. For an adaptive arm, the flips that fired
inside each phase are counted from the state file's monotonic stamps, aligned
by the journal's first arrival.

    score_walk.py --dir runs/local/eval-topology-walk --arm planner --tag seed7 \
        --segments "<spec>" [--rate 1.0] [--seed 7]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from narwhal.trace import make_trace, parse_segments


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        r for x in path.read_text().splitlines() if x.strip() and "meta" not in (r := json.loads(x))
    ]


def pct(xs: list[float], q: float) -> float:
    if not xs:
        return float("nan")
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="runs/local/stress")
    ap.add_argument("--arm", required=True)
    ap.add_argument("--tag", default="walk")
    ap.add_argument("--segments", required=True)
    ap.add_argument("--rate", type=float, default=1.0)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--ttft-slo", type=float, required=True)
    ap.add_argument("--tpot-slo", type=float, required=True)
    ap.add_argument(
        "--stations",
        default="",
        help="intended split per phase (4P2D,...); with the router "
        "log present, adds settle time and on-station share",
    )
    args = ap.parse_args()

    d = Path(args.dir)
    segments = parse_segments(args.segments)
    trace = make_trace(args.rate, args.seed, segments)
    bounds, t = [], 0.0
    for dur, _, _, _ in segments:
        bounds.append((t, t + dur))
        t += dur

    samples = load_jsonl(d / f"{args.arm}.{args.tag}.samples.jsonl")
    by_idx = {}
    for s in samples:
        if s["rid"].startswith("b"):
            by_idx[int(s["rid"][1:])] = s

    flips_at: list[float] = []
    state = d / f"{args.arm}.{args.tag}.state.after.json"
    journal = load_jsonl(d / f"{args.arm}.{args.tag}.journal.jsonl")
    if state.exists() and journal:
        st = json.loads(state.read_text())
        t0 = min(r["arrived"] for r in journal)  # monotonic clock of trace t=0
        flips_at = [f["at"] - t0 for f in st.get("flips", [])]

    print(f"{args.arm}.{args.tag}: {len(samples)} scored of {len(trace)} offered")
    header = (
        f"{'phase':>5} {'span':>11} {'offered':>8} {'attain':>7} "
        f"{'err':>4} {'short':>6} {'ttft>':>6} {'tpot>':>6} "
        f"{'ttft_p50':>9} {'ttft_p90':>9} {'flips':>6}"
    )
    print(header)
    total_met = 0
    for k, (lo, hi) in enumerate(bounds):
        idxs = [i for i, (at, _, _) in enumerate(trace) if lo <= at < hi]
        rows = [by_idx[i] for i in idxs if i in by_idx]
        err = sum(1 for r in rows if r["error"])
        short = sum(1 for r in rows if not r["error"] and r["output_len"] < r["wanted_len"])
        whole = [r for r in rows if not r["error"] and r["output_len"] >= r["wanted_len"]]
        ttft_miss = sum(1 for r in whole if (r["ttft_s"] or 9e9) > args.ttft_slo)
        tpot_miss = sum(
            1
            for r in whole
            if (r["ttft_s"] or 9e9) <= args.ttft_slo and (r["tpot_s"] or 0.0) > args.tpot_slo
        )
        met = len(whole) - ttft_miss - tpot_miss
        total_met += met
        ttfts = [r["ttft_s"] for r in rows if r["ttft_s"] is not None]
        nflips = sum(1 for at in flips_at if lo <= at < hi)
        attain = met / len(idxs) if idxs else float("nan")
        print(
            f"{k:>5} {int(lo):>5}-{int(hi):<5} {len(idxs):>8} {attain * 100:>6.1f}% "
            f"{err:>4} {short:>6} {ttft_miss:>6} {tpot_miss:>6} "
            f"{pct(ttfts, 0.5):>9.2f} {pct(ttfts, 0.9):>9.2f} {nflips:>6}"
        )
    print(f"overall: {total_met}/{len(trace)} = {total_met / len(trace) * 100:.1f}%")

    log = d / f"{args.arm}.{args.tag}.router.log"
    if args.stations and log.exists():
        stations = [int(x.strip()[0]) for x in args.stations.split(",")]
        line = re.compile(
            r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*loop \| "
            r"Lp=[\d.]+ Ld=[\d.]+ \| (\d)P\dD"
        )
        traj, t0 = [], None
        for raw in log.read_text().splitlines():
            m = line.search(raw)
            if not m:
                continue
            at = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            t0 = t0 or at
            traj.append(((at - t0).total_seconds(), int(m.group(2))))
        print("\nactuation (settle = boundary to first on-station pass):")
        for k, (lo, hi) in enumerate(bounds):
            if k >= len(stations):
                break
            want = stations[k]
            inside = [(tt, p) for tt, p in traj if lo <= tt < hi]
            settle = next((tt - lo for tt, p in inside if p == want), None)
            share = sum(1 for _, p in inside if p == want) / len(inside) * 100 if inside else 0.0
            settle_s = f"{settle:.0f}s" if settle is not None else "never"
            print(f"  phase {k} -> {want}P{6 - want}D: settled {settle_s}, on-station {share:.0f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
