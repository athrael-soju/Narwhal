"""Profile-based request prices and observed phase-load ratios."""

from __future__ import annotations

from collections.abc import Collection
from typing import TYPE_CHECKING

from ..profiling.model import Profile
from ..profiling.store import ProfileStore
from ..types import Instance, Phase, Request
from .health import DriftTracker
from .monitor import InstanceMonitor
from .prefill import prefill_seconds, resident_prefill_seconds

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
    warm: bool = True,
    probation: Collection[str] | None = None,
) -> Cost:
    """Return the lexicographic placement cost from Arrow §5.3 (arxiv.org/abs/2505.11916).

    `warm=False` prices prefill without cache evidence. `probation` is the health
    tracker's probation set, read from `health` when None.
    """
    profile = _profile(profiles, inst.iid)
    penalty = _penalty(inst.iid, health, probation)

    if request.phase is Phase.PREFILL:

        def price(r: Request) -> float:
            return prefill_seconds(profile, r) if warm else profile.prefill_time(r.input_len)

        resident = (
            resident_prefill_seconds(profile, inst)
            if warm
            else sum(price(r) for r in inst.prefill.values())
        )
        return (float(inst.decode_tokens()), resident + price(request) + penalty)

    return (
        float(inst.prefill_tokens()),
        _decode_price(
            profile,
            request,
            len(inst.decode),
            inst.decode_tokens(),
            correction=monitor.decode_correction(inst.iid),
            slo=slo,
            penalty=penalty,
        ),
    )


def decode_price(
    request: Request,
    iid: str,
    requests: int,
    tokens: int,
    *,
    monitor: InstanceMonitor,
    profiles: ProfileStore,
    slo: SLO,
    health: DriftTracker | None,
    probation: Collection[str] | None = None,
) -> float:
    """Return the decode placement price on `iid` beside `requests` residents of `tokens` tokens.

    The price equals the second cost element for an engine holding those residents.
    """
    return _decode_price(
        _profile(profiles, iid),
        request,
        requests,
        tokens,
        correction=monitor.decode_correction(iid),
        slo=slo,
        penalty=_penalty(iid, health, probation),
    )


def _profile(profiles: ProfileStore, iid: str) -> Profile:
    profile = profiles.get(iid)
    if profile is None:
        raise KeyError(f"no profile for instance {iid}; profile before scheduling")
    return profile


def _penalty(iid: str, health: DriftTracker | None, probation: Collection[str] | None) -> float:
    """Return the probation penalty in seconds; the decode price converts it to tokens."""
    if health is not None and iid in (health.probation_set() if probation is None else probation):
        return health.penalty_s
    return 0.0


def _decode_price(
    profile: Profile,
    request: Request,
    requests: int,
    tokens: int,
    *,
    correction: float,
    slo: SLO,
    penalty: float,
) -> float:
    headroom = profile.max_tokens(slo.tpot_s / correction, requests + 1)
    return float(tokens + request.length) - headroom + penalty / slo.tpot_s


def meets_slo(request: Request, cost: Cost, *, slo: SLO, ttft_margin: float = 0.0) -> bool:
    """Return whether the placement price fits its phase budget.

    The prefill TTFT budget widens by `ttft_margin`.
    """
    if request.phase is Phase.PREFILL:
        return cost[1] <= slo.ttft_s * (1.0 + ttft_margin)
    return cost[1] <= 0.0


def prefill_load(
    inst: Instance, *, monitor: InstanceMonitor, profiles: ProfileStore, slo: SLO
) -> float:
    """Return the larger of resident and last-interval prefill price, over the TTFT target."""
    profile = profiles.get(inst.iid)
    if profile is None:
        return 0.0
    resident = resident_prefill_seconds(profile, inst)
    return max(resident, monitor.mean_prefill_price(inst.iid)) / slo.ttft_s


def decode_load(
    inst: Instance, *, monitor: InstanceMonitor, profiles: ProfileStore, slo: SLO
) -> float:
    """Return normalized decode latency above the profiled idle floor.

    0 is the engine's idle cadence and 1.0 is the TPOT SLO.
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
        return observed / slo.tpot_s
    return max(0.0, observed - floor) / (slo.tpot_s - floor)
