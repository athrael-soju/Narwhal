"""One request through Algorithm 1, no engines required.

Algorithm 1 (Arrow §5.2) prices every instance for the phase at hand — profiled
latency plus the work already resident — and takes the cheapest one that still
meets the SLO. This script builds a four-instance fleet with synthetic
profiles, loads one instance, and shows the price changing the decision.

Run it from a checkout after `make setup`:

    .venv/bin/python examples/schedule_one_request.py
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from narwhal.monitor import InstanceMonitor
from narwhal.profiler import Profile, ProfileStore
from narwhal.scheduler import SLO, GlobalScheduler, Thresholds
from narwhal.types import Instance, Phase, Request, Role


def build_fleet() -> tuple[InstanceMonitor, GlobalScheduler]:
    """Two prefill and two decode instances, identically profiled.

    The profile is the quadratic prefill fit and the linear decode fit that
    `narwhal-profile` measures on a real engine (Arrow §5.2); here the numbers are
    invented but plausible for a mid-size model.
    """
    store = ProfileStore(Path(tempfile.mkdtemp()) / "profiles.json")
    monitor = InstanceMonitor(profiles=store)
    for k in range(4):
        iid = f"engine-{k}"
        monitor.add(
            Instance(
                iid=iid,
                url=f"http://node{k}:8000",
                role=Role.PREFILL if k < 2 else Role.DECODE,
            )
        )
        store.put(
            Profile(
                iid=iid,
                ttft_a=2e-8,  # quadratic term of prefill time in input tokens
                ttft_b=6e-5,  # linear term
                ttft_c=0.005,  # intercept
                tpot_slope=3e-6,  # decode interval per resident batch token
                tpot_intercept=0.012,  # zero-contention decode interval
            )
        )
    scheduler = GlobalScheduler(
        monitor,
        store,
        SLO(ttft_s=10.0, tpot_s=0.125),
        Thresholds(expand=1.0, shrink=0.5, cooldown_s=10.0),
    )
    return monitor, scheduler


def show(scheduler: GlobalScheduler, monitor: InstanceMonitor, request: Request) -> None:
    """§5.3's cost is a pair, compared lexicographically: pressure from the
    other phase first, then this leg's own cost. The second component is what
    `meets_slo` checks against the target."""
    for inst in monitor.instances.values():
        other, own = scheduler.cost(request, inst)
        print(
            f"  {inst.iid}  role={inst.role.value:<7} "
            f"cost=({other:9.1f}, {own:8.4f})  "
            f"meets_slo={scheduler.meets_slo(request, (other, own))}"
        )


def main() -> None:
    monitor, scheduler = build_fleet()

    # A 2,000-token prompt arrives. Algorithm 1 prices all four instances for
    # the prefill leg; on an idle fleet the prefill-labelled pair is cheapest.
    request = Request(rid="r1", input_len=2000, phase=Phase.PREFILL)
    print("prefill leg, idle fleet:")
    show(scheduler, monitor, request)
    chosen = scheduler.schedule(request)
    monitor.dispatched(chosen.iid, request)
    print(f"  -> Algorithm 1 placed the prefill leg on {chosen.iid}\n")

    # A second request finds engine-0 already carrying that work: the resident
    # prompt is priced into the queue term, so the other prefill instance wins.
    rival = Request(rid="r2", input_len=2000, phase=Phase.PREFILL)
    print("prefill leg, engine-0 now carrying r1:")
    show(scheduler, monitor, rival)
    chosen = scheduler.schedule(rival)
    print(f"  -> Algorithm 1 placed the prefill leg on {chosen.iid}\n")

    # The decode leg of r1 is priced over the decode pool the same way.
    request.phase = Phase.DECODE
    print("decode leg of r1:")
    show(scheduler, monitor, request)
    chosen = scheduler.schedule(request)
    print(f"  -> Algorithm 1 placed the decode leg on {chosen.iid}")
    print("     (a decode instance: the KV cache crosses, which Arrow §3.1 prices as cheap)")


if __name__ == "__main__":
    main()
