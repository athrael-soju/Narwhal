"""Bounded FIFO admission with synchronous reservation and cancellation cleanup."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Generic, TypeVar

T = TypeVar("T")


class QueueFull(Exception):
    """The admission queue is full."""


class PlacementRefused(Exception):
    """The cheapest placement exceeds the predictive admission budget."""

    def __init__(self, predicted_s: float) -> None:
        super().__init__(f"cheapest placement prices TTFT at {predicted_s:.2f}s")
        self.predicted_s = predicted_s


class QueueExpired(Exception):
    """A request exhausted its admission wait or original deadline."""


@dataclass(eq=False)
class _Waiter:
    wake: asyncio.Event = field(default_factory=asyncio.Event)


class AdmissionQueue(Generic[T]):
    """Wait in FIFO order until a callback can atomically reserve capacity.

    The callback returns None when capacity is unavailable. The queue head
    selects and reserves an engine synchronously, keeping its role stable.
    Call notify after capacity or engine eligibility changes.
    """

    def __init__(
        self,
        capacity: int,
        wait_s: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.capacity = capacity
        self.wait_s = wait_s
        self._clock = clock
        self._pending: OrderedDict[_Waiter, None] = OrderedDict()
        self.high_water = 0

    def __len__(self) -> int:
        return len(self._pending)

    def notify(self) -> None:
        """Wake the oldest waiter to recheck current capacity and roles."""
        if self._pending:
            next(iter(self._pending)).wake.set()

    async def acquire(self, reserve: Callable[[], T | None], *, deadline: float) -> T:
        """Reserve immediately or wait within both the queue and request clocks."""
        now = self._clock()
        if now >= deadline:
            raise QueueExpired
        if not self._pending:
            result = reserve()
            if result is not None:
                return result
        if len(self._pending) >= self.capacity:
            raise QueueFull
        waiter = _Waiter()
        self._pending[waiter] = None
        self.high_water = max(self.high_water, len(self._pending))
        expires = min(deadline, now + self.wait_s)
        try:
            while True:
                remaining = expires - self._clock()
                if remaining <= 0:
                    raise QueueExpired
                # Clear before checking capacity so a notification during the check
                # remains set when the waiter yields.
                waiter.wake.clear()
                if next(iter(self._pending)) is waiter:
                    result = reserve()
                    if result is not None:
                        return result
                try:
                    async with asyncio.timeout(remaining):
                        await waiter.wake.wait()
                except TimeoutError as exc:
                    raise QueueExpired from exc
        finally:
            # Removal also covers cancellation after the head was notified.
            del self._pending[waiter]
            self.notify()
