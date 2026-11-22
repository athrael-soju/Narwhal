"""Profile-based request prices and observed phase-load ratios."""

from __future__ import annotations

import math
from collections.abc import Callable, Collection
from typing import TYPE_CHECKING

from ..profiling.fitting import prefill_share
from ..profiling.model import Profile
from ..profiling.store import ProfileStore
from ..types import Instance, Phase, Request
from .health import DriftTracker
from .monitor import InstanceMonitor
from .prefill import prefill_seconds, resident_prefill_seconds
from .scheduler.occupancy import remaining_output

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


def decode_share(
    request: Request,
    inst: Instance,
    *,
    monitor: InstanceMonitor,
    profiles: ProfileStore,
    slo: SLO,
    health: DriftTracker | None,
    probation: Collection[str] | None = None,
) -> float:
    """Return the share of `inst`'s decode headroom that its residents and `request` would hold.

    Placement ranks the decode engines that meet the TPOT budget by this share. The headroom of
    engines on the same hardware differs by profiling noise, which the share keeps from
    outweighing their load.
    """
    profile = _profile(profiles, inst.iid)
    headroom = profile.max_tokens(
        slo.tpot_s / monitor.decode_correction(inst.iid), len(inst.decode) + 1
    )
    if headroom <= 0:
        return math.inf
    held = (
        inst.decode_tokens() + request.length + _penalty(inst.iid, health, probation) / slo.tpot_s
    )
    return held / headroom


def decode_engine_prefill(
    request: Request,
    inst: Instance,
    *,
    monitor: InstanceMonitor,
    profiles: ProfileStore,
    slo: SLO,
    expected_output: Callable[[Request], int],
) -> bool:
    """Return whether decode-role `inst` takes `request`'s prefill leg inside its envelope.

    The leg's prefill and the engine's resident prefill stall each resident decode stream. A
    resident with `left` output tokens at the corrected profile interval `d` decodes for
    `left * d` more seconds, which the profile's slowdown raises to
    `left * d + slowdown * stall`. Every resident with output left must keep its whole-output
    TPOT within `slo.tpot_s`, and `prefill_share` of the stall and the residents' mean
    `left * d` must stay within the measured share. An unknown remaining output keeps the
    engine out, and so does a leg whose prompt, with the engine's resident decode and prefill
    tokens, would pass the profile's measured decode range.
    """
    profile = profiles.get(inst.iid)
    if (
        profile is None
        or profile.decode_prefill_slowdown is None
        or profile.decode_prefill_max_share is None
        or profile.decode_max_kv_tokens is None
        or inst.decode_tokens() + inst.prefill_tokens() + request.input_len
        > profile.decode_max_kv_tokens
    ):
        return False
    stall = resident_prefill_seconds(profile, inst) + prefill_seconds(profile, request)
    interval = profile.token_interval(
        inst.decode_tokens(), len(inst.decode)
    ) * monitor.decode_correction(inst.iid)
    spans = []
    for resident in inst.decode.values():
        left = remaining_output(resident, expected_output)
        if left is None:
            return False
        spans.append(left * interval)
        if left == 0:
            continue
        decoding_s = (
            monitor.decode_elapsed(resident.rid)
            + left * interval
            + profile.decode_prefill_slowdown * stall
        )
        # The journal's TPOT divides by the output tokens less the first.
        if decoding_s / max(1, resident.output_len + left - 1) > slo.tpot_s:
            return False
    return not spans or (
        prefill_share(stall, math.fsum(spans) / len(spans)) <= profile.decode_prefill_max_share
    )


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
    """Return the larger of resident and last-interval prefill price, over the TTFT target.

    Both prices leave out a request whose decode leg waits for a seat.
    """
    profile = profiles.get(inst.iid)
    if profile is None:
        return 0.0
    resident = monitor.running_prefill_seconds(inst.iid)
    return max(resident, monitor.mean_prefill_price(inst.iid)) / slo.ttft_s


def decode_load(
    inst: Instance, *, monitor: InstanceMonitor, profiles: ProfileStore, slo: SLO
) -> float:
    """Return normalized decode latency above the profiled idle floor.

    0 is the engine's idle cadence and 1.0 is the TPOT SLO.
    """
    if not inst.decode:
        return 0.0
    observed = monitor.mean_token_interval(inst.iid)
    if not monitor.current_token_interval(inst.iid):
        observed = max(observed, monitor.stalled_gap(inst.iid))
    profile = profiles.get(inst.iid)
    floor = (
        profile.token_interval(0) * monitor.decode_correction(inst.iid)
        if profile is not None
        else 0.0
    )
    if floor >= slo.tpot_s:
        return observed / slo.tpot_s
    return max(0.0, observed - floor) / (slo.tpot_s - floor)
