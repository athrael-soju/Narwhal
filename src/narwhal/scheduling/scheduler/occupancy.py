"""Project decode holds and admit requests that fit decode capacity."""

from __future__ import annotations

import heapq
import math
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
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
    # Seated residents hold their slots from now.
    residents: tuple[DecodeSpan, ...]
    # Holds that take slots in handoff order: decode legs waiting for a seat inside their
    # engine and waiting decode requests from now, then requests in prefill without a decode
    # leg from their predicted prefill completion.
    queued: tuple[DecodeSpan, ...]
    # Seconds from now to each queued request's TTFT deadline.
    deadlines: tuple[float, ...]
    # Projected last token of each decode leg on its engine, by engine and request ID.
    ends: dict[tuple[str, str], float]
    # KV tokens of the decode legs on each engine.
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


class SlotWaitHeadroom:
    """Size the TTFT budget a request waiting for a decode slot keeps free.

    Each sample is how late a request that waited for a slot reached its first token against
    its projection at admission. The headroom is a high quantile of the recent samples.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        *,
        quantile: float = 0.95,
        window_s: float = 60.0,
        max_samples: int = 512,
        min_samples: int = 16,
    ) -> None:
        self._clock = clock
        self.quantile = quantile
        self.window_s = window_s
        self.min_samples = min_samples
        self._samples: deque[tuple[float, float]] = deque(maxlen=max_samples)
        self._seconds: float | None = 0.0

    def note(self, late_s: float) -> None:
        """Record first token minus its projection, in seconds, for one request."""
        self._samples.append((self._clock(), late_s))
        self._seconds = None

    def seconds(self, limit_s: float = math.inf) -> float:
        """Return the headroom, at most `limit_s` and 0 with too few recent samples."""
        cutoff = self._clock() - self.window_s
        if self._samples and self._samples[0][0] < cutoff:
            while self._samples and self._samples[0][0] < cutoff:
                self._samples.popleft()
            self._seconds = None
        if self._seconds is None:
            if len(self._samples) < self.min_samples:
                self._seconds = 0.0
            else:
                late = sorted(sample for _, sample in self._samples)
                self._seconds = max(0.0, late[min(len(late) - 1, int(self.quantile * len(late)))])
        return min(self._seconds, limit_s)


@dataclass(frozen=True)
class DecodeCheck:
    """The outcome of a request's decode admission check."""

    # The check the request fails, `slot_wait`, `kv_capacity` or `tpot`, else None.
    refusal: str | None
    # Projected seconds the request's first token waits for a decode slot.
    slot_wait_s: float = 0.0


def decode_occupancy(
    scheduler: GlobalScheduler,
    input_len: int,
    *,
    seats: Mapping[str, int] | None = None,
    expected_output: Callable[[Request], int] | None = None,
) -> DecodeOccupancy | None:
    """Project decode holds from now for a joining request of `input_len` prompt tokens.

    An engine's positive `seats` entry caps its slots.
    Returns None without live decode engines or with an engine lacking `decode_max_requests`.
    """
    engines = tuple(scheduler.live_instances(Role.DECODE))
    if not engines:
        return None
    estimate = expected_output or (lambda r: r.wanted_len)
    slots, tokens, steps, held = 0, 0.0, {}, {}
    seated: dict[str, list[Request]] = {}
    unseated: dict[str, list[Request]] = {}
    for inst in engines:
        profile = scheduler.profiles.get(inst.iid)
        if profile is None or profile.decode_max_requests is None:
            return None
        limit = profile.decode_max_requests
        seat = (seats or {}).get(inst.iid, 0)
        limit = min(limit, seat) if seat > 0 else limit
        token_limit = profile.decode_token_limit
        slots += limit
        tokens += float("inf") if token_limit is None else token_limit
        held[inst.iid] = inst.decode_tokens()
        # The engine batches its seated legs. A leg past its sequence limit waits inside it.
        seated[inst.iid], unseated[inst.iid] = scheduler.monitor.seat_legs(inst.iid)
        running = held[inst.iid] - sum(r.length for r in unseated[inst.iid])
        context = (running + input_len) / (len(seated[inst.iid]) + 1)
        batch = limit * context if token_limit is None else min(limit * context, token_limit)
        steps[inst.iid] = profile.token_interval(
            batch, limit
        ) * scheduler.monitor.decode_correction(inst.iid)
    step = sum(steps.values()) / len(steps)
    # Predicted prefill completion of each request in prefill. A request whose decode leg waits
    # for a seat starts its prefill when the leg is seated, so it queues behind no prefill and
    # delays none.
    gated = {r.rid for legs in unseated.values() for r in legs}
    prefilled: dict[str, float] = {}
    prefilling: list[tuple[float, Request]] = []
    own_prefill: dict[str, float] = {}
    for inst in scheduler.monitor.instances.values():
        prefill_profile = scheduler.profiles.get(inst.iid)
        done = 0.0
        for r in inst.prefill.values():
            seconds = prefill_seconds(prefill_profile, r) if prefill_profile is not None else 0.0
            if r.rid in gated:
                own_prefill[r.rid] = seconds
                continue
            done += seconds
            prefilled[r.rid] = done
            prefilling.append((done, r))

    def leg(r: Request, token_s: float) -> DecodeSpan:
        # A decode leg seated or waiting in the router while its request is in prefill, as with
        # a concurrent KV connector, holds its slot through that prefill, then decodes.
        _, last, kv = decode_span(r, prefilled.get(r.rid, 0.0), token_s, estimate)
        return 0.0, last, kv

    residents: list[DecodeSpan] = []
    ends: dict[tuple[str, str], float] = {}
    # Legs waiting for a seat, with the time their engine projects to seat them.
    seat_waits: list[tuple[float, DecodeSpan, Request]] = []
    for inst in engines:
        for r in seated[inst.iid]:
            resident = leg(r, steps[inst.iid])
            residents.append(resident)
            ends[inst.iid, r.rid] = resident[1]
        # Each waiting leg takes the engine's next freed seat, runs its prefill, then decodes.
        free = sorted(ends[inst.iid, r.rid] for r in seated[inst.iid])
        for r in unseated[inst.iid]:
            _, last, kv = decode_span(r, own_prefill.get(r.rid, 0.0), steps[inst.iid], estimate)
            start = free[0] if free else math.inf
            ends[inst.iid, r.rid] = start + last
            if free:
                heapq.heapreplace(free, start + last)
            seat_waits.append((start, (0.0, last, kv), r))
    seat_waits.sort(key=lambda row: row[0])
    waiting = [r for r in scheduler.monitor.waiting.values() if r.phase is Phase.DECODE]
    # A request with a decode leg takes no further slot when its prefill completes.
    legs = {rid for inst in scheduler.monitor.instances.values() for rid in inst.decode}
    legs.update(r.rid for r in waiting)
    handoffs = [
        *((hold, r) for _, hold, r in seat_waits),
        *((leg(r, step), r) for r in waiting),
    ] + [
        (decode_span(r, done, step, estimate), r)
        for done, r in sorted(prefilling, key=lambda row: row[0])
        if r.rid not in legs
    ]
    now = scheduler._clock()
    return DecodeOccupancy(
        engines,
        slots,
        tokens,
        step,
        tuple(residents),
        tuple(hold for hold, _ in handoffs),
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
    seats: Mapping[str, int] | None = None,
    expected_output: Callable[[Request], int] | None = None,
    post_seat_s: float = 0.0,
    slot_headroom_s: float = 0.0,
) -> bool:
    """Return whether `request` starts decode within its TTFT budget and fits decode."""
    return (
        decode_refusal(
            scheduler,
            request,
            ready_s=ready_s,
            ttft_s=ttft_s,
            ttft_margin=ttft_margin,
            seats=seats,
            expected_output=expected_output,
            post_seat_s=post_seat_s,
            slot_headroom_s=slot_headroom_s,
        )
        is None
    )


def decode_refusal(
    scheduler: GlobalScheduler,
    request: Request,
    *,
    ready_s: float = 0.0,
    ttft_s: float | None = None,
    ttft_margin: float = 0.0,
    seats: Mapping[str, int] | None = None,
    expected_output: Callable[[Request], int] | None = None,
    post_seat_s: float = 0.0,
    slot_headroom_s: float = 0.0,
) -> str | None:
    """Return the decode check `request` fails, `slot_wait`, `kv_capacity` or `tpot`, else None."""
    return decode_check(
        scheduler,
        request,
        ready_s=ready_s,
        ttft_s=ttft_s,
        ttft_margin=ttft_margin,
        seats=seats,
        expected_output=expected_output,
        post_seat_s=post_seat_s,
        slot_headroom_s=slot_headroom_s,
    ).refusal


def decode_check(
    scheduler: GlobalScheduler,
    request: Request,
    *,
    ready_s: float = 0.0,
    ttft_s: float | None = None,
    ttft_margin: float = 0.0,
    seats: Mapping[str, int] | None = None,
    expected_output: Callable[[Request], int] | None = None,
    post_seat_s: float = 0.0,
    slot_headroom_s: float = 0.0,
) -> DecodeCheck:
    """Check `request` against decode capacity and project its wait for a decode slot.

    `ttft_s` is the projected TTFT at prefill completion `ready_s`, `ready_s` by default.
    The request takes a free slot behind earlier handoffs, and `ttft_margin` widens the
    budget for its wait. `post_seat_s` is the part of `ready_s` that runs only once the
    request holds its slot, as its own prefill does with a concurrent KV connector. A request
    that waits for a slot keeps `slot_headroom_s` of its budget free. Peak KV tokens over its
    hold must fit the fleet, and some engine must meet the TPOT budget unless every idle
    engine misses it.
    """
    estimate = expected_output or (lambda r: r.wanted_len)
    occupancy = decode_occupancy(
        scheduler,
        request.input_len,
        seats=seats,
        expected_output=estimate,
    )
    if occupancy is None:
        return DecodeCheck(None)
    # The request needs its slot from `first`, and its hold covers the work that runs once it
    # is seated.
    first = max(0.0, ready_s - post_seat_s)
    _, last, kv = decode_span(request, ready_s, occupancy.step, estimate)
    started, ((start, end, request_kv),) = occupancy.schedule(occupancy.slots, ((first, last, kv),))
    wait = start - first
    if wait > 0 and not scheduler.meets_slo(
        replace(request, phase=Phase.PREFILL),
        (0.0, (ready_s if ttft_s is None else ttft_s) + wait + slot_headroom_s),
        ttft_margin=ttft_margin,
    ):
        return DecodeCheck("slot_wait", wait)
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
            return DecodeCheck("kv_capacity", wait)
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
        return DecodeCheck(None, wait)
    for inst in occupancy.engines:
        generating = [
            r.length for rid, r in inst.decode.items() if occupancy.ends[inst.iid, rid] > start
        ]
        # The request takes a seat beside the legs that hold the engine's other seats.
        sequences = scheduler.monitor.sequence_limit(inst.iid)
        if sequences > 0:
            del generating[sequences - 1 :]
        if len(generating) < len(inst.decode) and fits(inst.iid, len(generating), sum(generating)):
            return DecodeCheck(None, wait)
    refusal = "tpot" if any(fits(inst.iid, 0, 0) for inst in occupancy.engines) else None
    return DecodeCheck(refusal, wait)
