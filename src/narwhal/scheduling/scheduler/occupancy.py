"""Project decode holds and admit requests that fit decode capacity."""

from __future__ import annotations

import heapq
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from functools import cached_property
from typing import TYPE_CHECKING

from ...types import Instance, Phase, Request, Role
from ..prefill import prefill_seconds

if TYPE_CHECKING:
    from .placement import GlobalScheduler


# A request's decode hold: start, projected last token and KV tokens.
DecodeSpan = tuple[float, float, int]


def decode_span(
    request: Request, start: float, token_s: float, estimate: Callable[[Request], int]
) -> DecodeSpan:
    """Return `request`'s decode hold from `start` at `token_s` seconds per remaining token.

    Unknown remaining output holds decode indefinitely.
    """
    held = request.input_len + request.output_len
    expected = estimate(request)
    if expected > request.output_len:
        left = expected - request.output_len
    elif request.wanted_len > 0:
        left = max(0, request.wanted_len - request.output_len)
    else:
        return start, float("inf"), held
    return start, start + left * token_s, held + left


def peak_holds(
    spans: Iterable[DecodeSpan], start: float, end: float, *, held: int = 0, held_kv: float = 0.0
) -> tuple[int, float]:
    """Return peak holds and KV tokens from `start` to `end` above `held` and `held_kv`.

    A hold covers `[first, last)`, so a hold ending at an instant frees its slot for a hold
    starting then.
    """
    events = sorted(
        (t, order, kv)
        for first, last, kv in spans
        if (first <= start or first < end) and last > start
        for t, order in ((max(first, start), 1), (min(last, end), 0))
    )
    peak, peak_kv = held, held_kv
    for _, order, kv in events:
        held += 1 if order else -1
        held_kv += kv if order else -kv
        if order:
            peak, peak_kv = max(peak, held), max(peak_kv, held_kv)
    return peak, peak_kv


@dataclass(frozen=True)
class DecodeOccupancy:
    """Decode holds projected from now across the live decode engines."""

    engines: tuple[Instance, ...]
    slots: int
    tokens: float
    # Fleet mean full-slot token interval.
    step: float
    # Residents hold their slots from now.
    residents: tuple[DecodeSpan, ...]
    # Holds that take slots in handoff order: waiting decode requests from now,
    # then requests in prefill from their predicted prefill completion.
    queued: tuple[DecodeSpan, ...]
    # Seconds from now to each queued request's TTFT deadline.
    deadlines: tuple[float, ...]
    # Projected last token of each resident, by engine and request ID.
    ends: dict[tuple[str, str], float]
    # Resident KV tokens on each engine.
    held: dict[str, int]

    @cached_property
    def _sorted_ends(self) -> list[float]:
        return sorted(last for _, last, _ in self.residents)

    def schedule(
        self, slots: int, joins: tuple[DecodeSpan, ...] = ()
    ) -> tuple[tuple[DecodeSpan, ...], tuple[DecodeSpan, ...]]:
        """Start the queued holds and `joins` on `slots` slots as residents leave.

        Each hold takes the earliest free slot at or after its handoff. A joining hold
        follows the queued holds that reach decode no later. `joins` are in handoff order.
        """
        if slots > 0:
            # Idle slots free now, then the latest `slots` resident ends. The holds take
            # only the earliest free slots, one per hold.
            holds = len(self.queued) + len(joins)
            idle = max(0, slots - len(self.residents))
            if idle >= holds:
                free = [0.0] * holds
            else:
                first = max(0, len(self.residents) - slots)
                free = [0.0] * idle + self._sorted_ends[first : first + holds - idle]
        else:
            free = [math.inf]

        def take(hold: DecodeSpan) -> DecodeSpan:
            first, last, kv = hold
            start = max(first, free[0])
            end = start + (last - first)
            if slots > 0:
                heapq.heapreplace(free, end)
            return start, end, kv

        started: list[DecodeSpan] = []
        joined: list[DecodeSpan] = []
        for hold in self.queued:
            while len(joined) < len(joins) and joins[len(joined)][0] < hold[0]:
                joined.append(take(joins[len(joined)]))
            started.append(take(hold))
        joined += [take(hold) for hold in joins[len(joined) :]]
        return tuple(started), tuple(joined)


def decode_occupancy(
    scheduler: GlobalScheduler,
    input_len: int,
    *,
    concurrency: int = 0,
    expected_output: Callable[[Request], int] | None = None,
) -> DecodeOccupancy | None:
    """Project decode holds from now for a joining request of `input_len` prompt tokens.

    Returns None without live decode engines or with an engine lacking `decode_max_requests`.
    """
    engines = tuple(scheduler.live_instances(Role.DECODE))
    if not engines:
        return None
    estimate = expected_output or (lambda r: r.wanted_len)
    slots, tokens, steps, held = 0, 0.0, {}, {}
    for inst in engines:
        profile = scheduler.profiles.get(inst.iid)
        if profile is None or profile.decode_max_requests is None:
            return None
        limit = profile.decode_max_requests
        limit = min(limit, concurrency) if concurrency > 0 else limit
        token_limit = profile.decode_token_limit
        slots += limit
        tokens += float("inf") if token_limit is None else token_limit
        held[inst.iid] = inst.decode_tokens()
        context = (held[inst.iid] + input_len) / (len(inst.decode) + 1)
        batch = limit * context if token_limit is None else min(limit * context, token_limit)
        steps[inst.iid] = profile.token_interval(
            batch, limit
        ) * scheduler.monitor.decode_correction(inst.iid)
    step = sum(steps.values()) / len(steps)
    residents: list[DecodeSpan] = []
    ends: dict[tuple[str, str], float] = {}
    for inst in engines:
        for rid, r in inst.decode.items():
            resident = decode_span(r, 0.0, steps[inst.iid], estimate)
            residents.append(resident)
            ends[inst.iid, rid] = resident[1]
    handoffs = [(0.0, r) for r in scheduler.monitor.waiting.values() if r.phase is Phase.DECODE]
    prefilling: list[tuple[float, Request]] = []
    for inst in scheduler.monitor.instances.values():
        prefill_profile = scheduler.profiles.get(inst.iid)
        done = 0.0
        for r in inst.prefill.values():
            if prefill_profile is not None:
                done += prefill_seconds(prefill_profile, r)
            prefilling.append((done, r))
    handoffs += sorted(prefilling, key=lambda row: row[0])
    now = scheduler._clock()
    return DecodeOccupancy(
        engines,
        slots,
        tokens,
        step,
        tuple(residents),
        tuple(decode_span(r, ready, step, estimate) for ready, r in handoffs),
        tuple(
            scheduler.slo.ttft_s - (now - r.arrived_at if r.arrived_at is not None else 0.0)
            for _, r in handoffs
        ),
        ends,
        held,
    )


def decode_admits(
    scheduler: GlobalScheduler,
    request: Request,
    *,
    ready_s: float = 0.0,
    ttft_s: float | None = None,
    ttft_margin: float = 0.0,
    concurrency: int = 0,
    expected_output: Callable[[Request], int] | None = None,
) -> bool:
    """Return whether `request` starts decode within its TTFT budget and fits decode.

    `ttft_s` is the projected TTFT at prefill completion `ready_s`, `ready_s` by default.
    The request takes a free slot behind earlier handoffs, and `ttft_margin` widens the
    budget for its wait. Peak KV tokens over its hold must fit the fleet, and some engine
    must meet the TPOT budget unless every idle engine misses it.
    """
    estimate = expected_output or (lambda r: r.wanted_len)
    occupancy = decode_occupancy(
        scheduler, request.input_len, concurrency=concurrency, expected_output=estimate
    )
    if occupancy is None:
        return True
    started, ((start, end, request_kv),) = occupancy.schedule(
        occupancy.slots, (decode_span(request, ready_s, occupancy.step, estimate),)
    )
    wait = start - ready_s
    if wait > 0 and not scheduler.meets_slo(
        replace(request, phase=Phase.PREFILL),
        (0.0, (ready_s if ttft_s is None else ttft_s) + wait),
        ttft_margin=ttft_margin,
    ):
        return False
    # A hold ending as it starts adds no peak, and KV that fits with every hold fits every peak.
    end = start if end == math.inf else end
    if (
        end != start
        and request_kv
        + sum(kv for _, _, kv in occupancy.residents)
        + sum(kv for _, _, kv in started)
        > occupancy.tokens
    ):
        peak, peak_kv = peak_holds(
            (*occupancy.residents, *started), start, end, held=1, held_kv=request_kv
        )
        if peak > 1 and peak_kv > occupancy.tokens:
            return False
    decode = replace(request, phase=Phase.DECODE)
    health = scheduler.health
    probation = health.probation_set() if health is not None else None

    def fits(iid: str, requests: int, tokens: int) -> bool:
        price = scheduler.decode_price(decode, iid, requests, tokens, probation=probation)
        return scheduler.meets_slo(decode, (0.0, price))

    # Fewer residents never raise the price, so an engine that fits all its residents
    # fits those generating at `start`.
    if any(
        fits(inst.iid, len(inst.decode), occupancy.held[inst.iid]) for inst in occupancy.engines
    ):
        return True
    for inst in occupancy.engines:
        generating = [
            r.length for rid, r in inst.decode.items() if occupancy.ends[inst.iid, rid] > start
        ]
        if len(generating) < len(inst.decode) and fits(inst.iid, len(generating), sum(generating)):
            return True
    return not any(fits(inst.iid, 0, 0) for inst in occupancy.engines)
