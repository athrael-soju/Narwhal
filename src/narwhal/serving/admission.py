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
    """The cheapest placement exceeds the predictive admission budget.

    `cause` is `queue`, `prompt` or `aggregate_unpriced` for the TTFT check, or the failed
    decode check: `slot_wait`, `kv_capacity` or `tpot`.
    """

    def __init__(self, predicted_s: float, *, cause: str) -> None:
        super().__init__(f"{cause} refusal; cheapest placement prices TTFT at {predicted_s:.2f}s")
        self.predicted_s = predicted_s
        self.cause = cause


class QueueExpired(Exception):
    """A request exhausted its admission wait or the deadline its caller set.

    `at_deadline` records whether the caller's deadline, not the queue's wait limit,
    ended the wait. An event-loop timer can fire shortly before that deadline on the
    router clock, so callers classify the expiry from this flag.
    """

    def __init__(self, *, at_deadline: bool = False) -> None:
        super().__init__()
        self.at_deadline = at_deadline


@dataclass(eq=False)
class _Waiter:
    wake: asyncio.Event = field(default_factory=asyncio.Event)


class AdmissionQueue(Generic[T]):
    """Wait in FIFO order until a callback can atomically reserve capacity.

    The callback returns None when capacity is unavailable.
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
            raise QueueExpired(at_deadline=True)
        if not self._pending:
            result = reserve()
            if result is not None:
                return result
        if len(self._pending) >= self.capacity:
            raise QueueFull
        waiter = _Waiter()
        self._pending[waiter] = None
        self.high_water = max(self.high_water, len(self._pending))
        at_deadline = deadline <= now + self.wait_s
        expires = min(deadline, now + self.wait_s)
        try:
            while True:
                remaining = expires - self._clock()
                if remaining <= 0:
                    raise QueueExpired(at_deadline=at_deadline)
                # A notification during the capacity check stays set for the next wait.
                waiter.wake.clear()
                if next(iter(self._pending)) is waiter:
                    result = reserve()
                    if result is not None:
                        return result
                try:
                    async with asyncio.timeout(remaining):
                        await waiter.wake.wait()
                except TimeoutError as exc:
                    raise QueueExpired(at_deadline=at_deadline) from exc
        finally:
            # Removal also covers cancellation after the head was notified.
            del self._pending[waiter]
            self.notify()
