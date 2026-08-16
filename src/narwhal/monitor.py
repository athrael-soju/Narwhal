"""Per-instance state, derived from scheduler events rather than by scraping (Arrow §5.2)."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from .profiler import ProfileStore
from .types import Instance, Phase, Request, Role


@dataclass
class _Window:
    """Inter-token gaps for one update interval, and the last completed one.

    Arrow §5.5 reads the load off "the average latency of tokens generated between the
    update interval", which is a completed interval. `published` holds that value
    for the whole of the next one, so every reader sees the same number however
    often it asks.
    """

    total_s: float = 0.0
    count: int = 0
    published: float = 0.0

    def add(self, gap: float) -> None:
        """Register an instance and open its pricing windows."""
        self.total_s += gap
        self.count += 1

    def mean(self) -> float:
        return self.published

    def reset(self) -> None:
        if self.count:
            self.published = self.total_s / self.count
        self.total_s = 0.0
        self.count = 0


@dataclass
class _PriceWindow:
    """Resident prefill price integrated over one update interval.

    The resident set empties between arrivals at low rates, so a value read
    off the set at pass time aliases to zero. The integral is what was
    resident over the interval, so a pass between arrivals still sees the
    interval's work.
    """

    integral: float = 0.0
    current: float = 0.0
    anchor: float = 0.0
    window_start: float = 0.0
    published: float = 0.0

    def touch(self, now: float) -> None:
        self.integral += self.current * (now - self.anchor)
        self.anchor = now

    def roll(self, now: float) -> None:
        self.touch(now)
        span = now - self.window_start
        if span > 0.0:
            self.published = self.integral / span
        self.integral = 0.0
        self.window_start = now


class InstanceMonitor:
    """Live per-instance state. `clock` is injected so a replay can drive it.

    `profiles` prices resident prefill work for the interval average; without
    it the published prefill price stays 0 and readers fall back to the
    resident set alone.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        profiles: ProfileStore | None = None,
    ) -> None:
        self._clock = clock
        self.profiles = profiles
        self.instances: dict[str, Instance] = {}
        self._windows: dict[str, _Window] = {}
        self._prices: dict[str, _PriceWindow] = {}
        self._last_token: dict[str, float] = {}
        # Requests whose decode leg has produced a token. The gap before that
        # token spans the KV transfer and the decode queue (Arrow §4.3's `q2 + c +
        # q3`), which measures the path into the instance rather than the
        # instance, so it is not a generation gap.
        self._decode_started: set[str] = set()

    # -- registration ---------------------------------------------------

    def add(self, instance: Instance) -> None:
        """Register an instance and open its pricing windows."""
        now = self._clock()
        self.instances[instance.iid] = instance
        self._windows[instance.iid] = _Window()
        self._prices[instance.iid] = _PriceWindow(anchor=now, window_start=now)

    def pool(self, role: Role) -> list[Instance]:
        """The instances carrying `role` right now."""
        return [i for i in self.instances.values() if i.role == role]

    # -- events ---------------------------------------------------------

    def _reprice(self, iid: str) -> None:
        """Close the segment the old resident set priced, and price the new one."""
        w = self._prices[iid]
        w.touch(self._clock())
        profile = self.profiles.get(iid) if self.profiles else None
        if profile is None:
            w.current = 0.0
            return
        inst = self.instances[iid]
        w.current = sum(profile.prefill_time(r.input_len) for r in inst.prefill.values())

    def dispatched(self, iid: str, request: Request) -> None:
        """A request landed: track it, and reprice resident prefill work."""
        inst = self.instances[iid]
        if request.phase is Phase.PREFILL:
            inst.prefill[request.rid] = request
            request.prefill_instance = iid
            self._reprice(iid)
        else:
            inst.decode[request.rid] = request

    def first_token(self, iid: str, rid: str) -> None:
        """o1 exists: leave the prefill set and start the TPOT clock (Arrow §4.2)."""
        self.instances[iid].prefill.pop(rid, None)
        self._last_token[rid] = self._clock()
        self._reprice(iid)

    def output_token(self, iid: str, rid: str) -> None:
        """One decode token. The gap is credited to the instance that served it.

        Only gaps between tokens this instance generated count: the first
        decode token's gap reaches back to o1 on the prefill instance, across
        the transfer and the queue, and charging it here makes an overloaded
        prefill stage read as decode pressure.
        """
        now = self._clock()
        prev = self._last_token.get(rid)
        if prev is not None and rid in self._decode_started:
            self._windows[iid].add(now - prev)
        self._decode_started.add(rid)
        self._last_token[rid] = now
        req = self.instances[iid].decode.get(rid)
        if req is not None:
            req.output_len += 1

    def finished(self, iid: str, rid: str) -> None:
        """A request left, however it ended; its tracking state goes with it."""
        inst = self.instances[iid]
        inst.decode.pop(rid, None)
        had_prefill = inst.prefill.pop(rid, None)
        self._last_token.pop(rid, None)
        self._decode_started.discard(rid)
        if had_prefill is not None:
            self._reprice(iid)

    # -- readings -------------------------------------------------------

    def mean_token_interval(self, iid: str) -> float:
        """Mean inter-token latency since the last `roll_interval` (Arrow §5.5)."""
        return self._windows[iid].mean()

    def stalled_gap(self, iid: str) -> float:
        """Longest gap a generating decode request has waited so far.

        Arrow §5.5 averages over the tokens generated in the interval, and an instance
        that generated none has an empty average. A request that produced tokens
        and then stopped has an open inter-token gap at least this long, which
        is the floor that empty average lacks. A request still waiting for its
        first decode token is excluded: that wait is the transfer and the queue,
        bounded by the engine client's first-token deadline, not a stall of
        this instance.
        """
        now = self._clock()
        gaps = [
            now - self._last_token[rid]
            for rid in self.instances[iid].decode
            if rid in self._last_token and rid in self._decode_started
        ]
        return max(gaps, default=0.0)

    def mean_prefill_price(self, iid: str) -> float:
        """Resident prefill seconds, averaged over the last completed interval."""
        return self._prices[iid].published

    def roll_interval(self) -> None:
        """Close the update interval. One call per monitoring loop pass."""
        now = self._clock()
        for w in self._windows.values():
            w.reset()
        for p in self._prices.values():
            p.roll(now)
