"""Measure how late the router's event loop wakes and how long token counting takes."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from statistics import median
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .router import NarwhalRouter

LAG_PROBE_S = 0.05
SIZING_WINDOW_S = 2.0
# Delay at this share of the TTFT budget leaves admitted work unable to meet it.
SATURATED_TTFT_SHARE = 0.25


async def measure_loop_lag(router: NarwhalRouter) -> None:
    """Record each probe's wake-up lateness in `router.loop_lag_s`."""
    loop = asyncio.get_running_loop()
    while True:
        started = loop.time()
        await asyncio.sleep(LAG_PROBE_S)
        router.loop_lag_s = max(0.0, loop.time() - started - LAG_PROBE_S)


class RecentDelays:
    """Keep delays observed within a trailing window."""

    def __init__(self, window_s: float, clock: Callable[[], float]) -> None:
        self.window_s, self._clock = window_s, clock
        self._rows: deque[tuple[float, float]] = deque()

    def add(self, delay_s: float) -> None:
        """Record one delay at the current time."""
        self._rows.append((self._clock(), delay_s))

    def __len__(self) -> int:
        self._prune()
        return len(self._rows)

    def median(self) -> float:
        """Return the median delay inside the window, 0 when it holds none."""
        self._prune()
        return median(delay for _, delay in self._rows) if self._rows else 0.0

    def _prune(self) -> None:
        cutoff = self._clock() - self.window_s
        while self._rows and self._rows[0][0] < cutoff:
            self._rows.popleft()
