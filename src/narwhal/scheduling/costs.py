"""Profile-based request prices and observed phase-load ratios."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..profiling.store import ProfileStore
from ..types import Instance, Phase, Request
from .health import DriftTracker
from .monitor import InstanceMonitor

if TYPE_CHECKING:
    from .control import SLO

# Costs are ordered lexicographically.
Cost = tuple[float, float]


def cost(
    request: Request,
    inst: Instance,
    *,
    monitor: InstanceMonitor,
    profiles: ProfileStore,
    slo: SLO,
    health: DriftTracker | None,
) -> Cost:
    """Compute the request's lexicographic placement cost, based on Arrow §5.3.

    Arrow: https://arxiv.org/abs/2505.11916

    Prefill: `(sum L(rd) for rd in D, sum T(rp, i) for rp in P + {r})`.
    Decode:  `(sum L(rp) for rp in P, sum L(rd) for rd in D + {r} - MT(i))`.
    """
    profile = profiles.get(inst.iid)
    if profile is None:
        raise KeyError(f"no profile for instance {inst.iid}; profile before scheduling")

    # Convert the probation penalty to tokens for decode comparisons.
    penalty = 0.0
    if health is not None and inst.iid in health.probation_set():
        penalty = health.penalty_s

    if request.phase is Phase.PREFILL:
        resident = sum(profile.prefill_time(r.input_len) for r in inst.prefill.values())
        own = profile.prefill_time(request.input_len)
        return (float(inst.decode_tokens()), resident + own + penalty)

    correction = monitor.decode_correction(inst.iid)
    headroom = profile.max_tokens(
        slo.tpot_s / correction,
        len(inst.decode) + 1,
    )
    return (
        float(inst.prefill_tokens()),
        float(inst.decode_tokens() + request.length) - headroom + penalty / slo.tpot_s,
    )


def meets_slo(request: Request, cost: Cost, *, slo: SLO, ttft_margin: float = 0.0) -> bool:
    """Test the placement price against its phase budget.

    Prefill admission expands its TTFT boundary by `ttft_margin` to absorb
    pricing noise.
    """
    if request.phase is Phase.PREFILL:
        return cost[1] <= slo.ttft_s * (1.0 + ttft_margin)
    return cost[1] <= 0.0


def prefill_load(
    inst: Instance, *, monitor: InstanceMonitor, profiles: ProfileStore, slo: SLO
) -> float:
    """Return prefill load as a ratio to the TTFT target.

    The interval average preserves work completed between monitoring passes.
    The larger of current and interval load protects the shrink trigger from
    low-rate aliasing.
    """
    profile = profiles.get(inst.iid)
    if profile is None:
        return 0.0
    resident = sum(profile.prefill_time(r.input_len) for r in inst.prefill.values())
    return max(resident, monitor.mean_prefill_price(inst.iid)) / slo.ttft_s


def decode_load(
    inst: Instance, *, monitor: InstanceMonitor, profiles: ProfileStore, slo: SLO
) -> float:
    """Return normalized decode latency above the profiled idle floor.

    Open inter-token gaps provide a floor when an interval emits no token.
    Zero represents the engine's idle cadence and 1.0 represents the TPOT SLO.
    """
    if not inst.decode:
        return 0.0
    observed = max(
        monitor.mean_token_interval(inst.iid),
        monitor.stalled_gap(inst.iid),
    )
    profile = profiles.get(inst.iid)
    floor = (
        profile.token_interval(0) * monitor.decode_correction(inst.iid)
        if profile is not None
        else 0.0
    )
    if floor >= slo.tpot_s:
        # Use the raw ratio when the idle floor already misses the SLO.
        return observed / slo.tpot_s
    return max(0.0, observed - floor) / (slo.tpot_s - floor)
