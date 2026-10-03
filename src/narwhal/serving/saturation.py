"""Measure how late the router's event loop wakes and how long request sizing takes."""

from __future__ import annotations

import asyncio
from bisect import bisect_left, insort
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .router.routing import NarwhalRouter

LAG_PROBE_S = 0.05
SIZING_WINDOW_S = 2.0
# Fewer sizing delays in the window leave the sizing signal at zero.
SIZING_MIN_SAMPLES = 8
# Delay at this share of the TTFT budget leaves admitted work unable to meet it.
SATURATED_TTFT_SHARE = 0.25


async def measure_loop_lag(router: NarwhalRouter) -> None:
    """Record each probe's wake-up lateness in `router.loop_lag_s`."""
    loop = asyncio.get_running_loop()
    while True:
        started = loop.time()
        await asyncio.sleep(LAG_PROBE_S)
        router.loop_lag_s = max(0.0, loop.time() - started - LAG_PROBE_S)


def saturated(router: NarwhalRouter) -> bool:
    """Return whether loop lag or recent request sizing takes a quarter of the TTFT budget."""
    budget = SATURATED_TTFT_SHARE * router.scheduler.slo.ttft_s
    return router.loop_lag_s >= budget or router.sizing_delays.median() >= budget


class RecentDelays:
    """Keep delays observed within a trailing window, in arrival and sorted order."""

    def __init__(
        self, window_s: float, clock: Callable[[], float], *, min_samples: int = 1
    ) -> None:
        self.window_s, self._clock, self.min_samples = window_s, clock, min_samples
        self._rows: deque[tuple[float, float]] = deque()
        self._sorted: list[float] = []

    def add(self, delay_s: float) -> None:
        """Record one delay at the current time."""
        self._rows.append((self._clock(), delay_s))
        insort(self._sorted, delay_s)

    def __len__(self) -> int:
        self._prune()
        return len(self._rows)

    def median(self) -> float:
        """Return the median delay inside the window, 0 below `min_samples` delays."""
        self._prune()
        count = len(self._sorted)
        if count < self.min_samples or count == 0:
            return 0.0
        middle = count // 2
        if count % 2:
            return self._sorted[middle]
        return (self._sorted[middle - 1] + self._sorted[middle]) / 2

    def _prune(self) -> None:
        cutoff = self._clock() - self.window_s
        while self._rows and self._rows[0][0] < cutoff:
            _, delay = self._rows.popleft()
            del self._sorted[bisect_left(self._sorted, delay)]
