"""Measure how late the router's event loop wakes."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .router import NarwhalRouter

LAG_PROBE_S = 0.05
# Loop lag at this share of the TTFT budget leaves admitted work unable to meet it.
SATURATED_TTFT_SHARE = 0.25


async def measure_loop_lag(router: NarwhalRouter) -> None:
    """Record each probe's wake-up lateness in `router.loop_lag_s`."""
    loop = asyncio.get_running_loop()
    while True:
        started = loop.time()
        await asyncio.sleep(LAG_PROBE_S)
        router.loop_lag_s = max(0.0, loop.time() - started - LAG_PROBE_S)
