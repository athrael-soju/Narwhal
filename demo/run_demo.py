#!/usr/bin/env python3
"""Arrow against a static prefill/decode split, on one replayed workload.

The shape of §6.3's ablation: same trace, same instances, SLO attainment as the
metric, and the only difference is whether roles may move. The trace runs
decode-heavy, prefill-heavy, then back, so a static split is wrong for two
thirds of it.
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from narwhal.monitor import InstanceMonitor
from narwhal.profiler import Profile, ProfileStore
from narwhal.scheduler import SLO, GlobalScheduler, Thresholds
from narwhal.sim import Fleet, TraceEntry
from narwhal.types import Instance, Phase, Role

# §6.1's fleet is 4 prefill plus 4 decode.
N_INSTANCES = 8
TTFT_SLO = 10.0  # Table 1, Azure Code / Qwen3-32B
TPOT_SLO = 0.125  # Table 1, same row
DT = 0.01


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def make_trace(rate: float, seed: int = 7) -> list[TraceEntry]:
    """Three segments whose P/D load ratio swings.

    `rate` is swept by the caller: §6.1 reports the highest rate that holds the
    attainment target, not attainment at one rate.
    """
    rng = random.Random(seed)
    trace: list[TraceEntry] = []
    n = 0
    # (duration_s, input range, output range)
    segments = [
        (30.0, (200, 400), (400, 600)),  # decode-heavy
        (30.0, (6000, 9000), (30, 60)),  # prefill-heavy
        (30.0, (200, 400), (400, 600)),  # decode-heavy again
    ]
    t = 0.0
    for dur, isl, osl in segments:
        end = t + dur
        while t < end:
            t += rng.expovariate(rate)
            if t >= end:
                break
            n += 1
            trace.append(
                TraceEntry(
                    at=t,
                    rid=f"r{n}",
                    input_len=rng.randint(*isl),
                    output_len=rng.randint(*osl),
                )
            )
    return trace


def build(tmp: Path, n_prefill: int, tag: str):
    clock = Clock()
    store = ProfileStore(tmp / f"profiles-{tag}.json")
    mon = InstanceMonitor(clock=clock, profiles=store)
    for k in range(N_INSTANCES):
        iid = f"i{k}"
        mon.add(
            Instance(
                iid=iid,
                url=f"http://{iid}",
                role=Role.PREFILL if k < n_prefill else Role.DECODE,
            )
        )
        # Homogeneous fleet, so any difference between arms is scheduling.
        store.put(
            Profile(
                iid=iid,
                ttft_a=2e-8,  # quadratic in input length (Arrow §3.1)
                ttft_b=6e-5,
                ttft_c=0.005,
                tpot_slope=3e-6,  # linear in batch tokens (Arrow §3.1)
                tpot_intercept=0.012,
            )
        )
    sched = GlobalScheduler(
        mon,
        store,
        SLO(ttft_s=TTFT_SLO, tpot_s=TPOT_SLO),
        Thresholds(expand=1.0, shrink=0.5, cooldown_s=2.0),
        clock=clock,
    )
    fleet = Fleet(mon, store, clock, kv_transfer_s=0.05, dt=DT)
    return clock, mon, sched, fleet


def run(trace: list[TraceEntry], tmp: Path, mode: str, n_prefill: int, tag: str):
    clock, mon, sched, fleet = build(tmp, n_prefill, tag)
    pending = sorted(trace, key=lambda e: e.at)
    idx = 0
    horizon = pending[-1].at + 120.0
    next_monitor = 0.0

    while clock.t < horizon:
        while idx < len(pending) and pending[idx].at <= clock.t:
            entry = pending[idx]
            idx += 1
            probe = TraceEntry(entry.at, entry.rid, entry.input_len, entry.output_len)
            from narwhal.types import Request

            r = Request(rid=probe.rid, input_len=probe.input_len, phase=Phase.PREFILL)
            if mode == "arrow":
                target = sched.schedule(r)
            elif mode == "aggregated":
                every = list(mon.instances.values())
                target = min(every, key=lambda i: i.prefill_tokens() + i.decode_tokens())
            else:
                pool = mon.pool(Role.PREFILL)
                target = min(pool, key=lambda i: i.prefill_tokens())
            fleet.admit(probe, target.iid)

        for rid in list(fleet.awaiting_decode):
            fleet.awaiting_decode.remove(rid)
            live = fleet.live[rid]
            live.request.phase = Phase.DECODE
            if mode == "arrow":
                target = sched.schedule(live.request)
            elif mode == "aggregated":
                # Decode where it prefilled: no transfer, no boundary.
                target = mon.instances[live.request.prefill_instance]
            else:
                pool = mon.pool(Role.DECODE)
                target = min(pool, key=lambda i: i.decode_tokens())
            fleet.dispatch_decode(rid, target.iid)

        fleet.step()

        if mode == "arrow" and clock.t >= next_monitor:
            sched.monitoring_pass()
            next_monitor = clock.t + 1.0

        clock.t += DT
        if idx >= len(pending) and not any(
            live.finished_at is None for live in fleet.live.values()
        ):
            break

    frac, met, total = fleet.attainment(TTFT_SLO, TPOT_SLO)
    return frac, met, total, len(sched.flips), fleet


def main() -> int:
    global N_INSTANCES

    ap = argparse.ArgumentParser(description="Arrow against the static topology spectrum")
    ap.add_argument(
        "--instances",
        type=int,
        default=N_INSTANCES,
        help="fleet size; 8 reproduces the Arrow paper, 6 matches the AMD fleet",
    )
    ap.add_argument("--rates", default="0.6,1.0,1.6,2.4,3.2,4.4,5.4,6.4")
    args = ap.parse_args()
    N_INSTANCES = args.instances
    if N_INSTANCES < 2:
        print("need at least two instances to have a split at all")
        return 2

    tmp = Path(__file__).resolve().parent.parent / "runs"
    tmp.mkdir(exist_ok=True)

    TARGET = 0.90  # §6.1
    # Table 1 gives 0.6-6.4 req/s for this trace and model.
    rates = [float(x) for x in args.rates.split(",") if x.strip()]

    n = N_INSTANCES
    # The whole spectrum, not one hand-picked baseline.
    start = n // 2
    arms = [(f"static {p}P{n - p}D", "static", p) for p in range(1, n)]
    arms.append((f"aggregated {n}x", "aggregated", start))
    arms.append(("arrow (adaptive)", "arrow", start))

    print(f"fleet: {N_INSTANCES} stateless instances | SLO ttft<={TTFT_SLO}s tpot<={TPOT_SLO}s")
    print("trace: three segments, P/D load swinging; rate swept per §6.1")
    print()
    header = f"{'arm':<20}" + "".join(f"{r:>7.1f}" for r in rates) + f"{'sustained':>12}"
    print(header)
    print("-" * len(header))

    sustained: dict[str, float] = {}
    for label, mode, n_prefill in arms:
        cells = []
        best = 0.0
        for rate in rates:
            trace = make_trace(rate)
            frac, _, _, _, _ = run(
                trace, tmp, mode=mode, n_prefill=n_prefill, tag=f"{label}-{rate}"
            )
            cells.append(f"{frac * 100:>6.0f}%")
            if frac >= TARGET:
                best = rate
        sustained[label] = best
        shown = f"{best:.1f} req/s" if best else "none"
        print(f"{label:<20}" + "".join(cells) + f"{shown:>12}")

    print()
    arrow = sustained["arrow (adaptive)"]
    best_static = max(v for k, v in sustained.items() if k != "arrow (adaptive)")
    if best_static > 0:
        print(
            f"arrow sustains {arrow / best_static:.2f}x the best static split "
            f"at {TARGET:.0%} attainment ({arrow:.1f} against {best_static:.1f} req/s)"
        )
    else:
        print(
            f"no static split reaches {TARGET:.0%} at any swept rate; "
            f"arrow sustains {arrow:.1f} req/s"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
