"""Asyncio task control for profiling runs."""

from __future__ import annotations

import asyncio
from typing import Any


async def cancel_tasks(tasks: list[asyncio.Task[Any]]) -> None:
    """Cancel `tasks` and wait for each to finish."""
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
