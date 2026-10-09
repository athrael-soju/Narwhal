"""Bounded FIFO admission with same-pass handoff, rechecks and cancellation cleanup."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Generic, TypeVar, cast

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
class _Waiter(Generic[T]):
    reserve: Callable[[], T | None]
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    granted: bool = False
    result: T | None = None
    error: Exception | None = None


class AdmissionQueue(Generic[T]):
    """Wait in FIFO order until a callback can atomically reserve capacity.

    The callback returns None when capacity is unavailable. A waiter's callback runs in
    the pass that frees capacity, so a reservation it makes belongs to its caller even
    when the waiting task is cancelled before it resumes.
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
        self._pending: OrderedDict[_Waiter[T], None] = OrderedDict()
        self._passing = False
        self._again = False
        self.high_water = 0

    def __len__(self) -> int:
        return len(self._pending)

    def notify(self) -> None:
        """Hand capacity to waiters in FIFO order until the oldest one cannot reserve.

        Each granted waiter leaves the queue in this pass, so the next reservation sees
        its claim. The oldest remaining waiter wakes to rerun its check and expiry.
        """
        if self._passing:
            self._again = True
            return
        self._passing = True
        try:
            self._again = True
            while self._again:
                self._again = False
                self._pass()
        finally:
            self._passing = False

    def _pass(self) -> None:
        while self._pending:
            waiter = next(iter(self._pending))
            try:
                result = waiter.reserve()
            except Exception as exc:
                waiter.error = exc
            else:
                if result is None:
                    waiter.wake.set()
                    return
                waiter.granted, waiter.result = True, result
            del self._pending[waiter]
            waiter.wake.set()

    def wake_all(self) -> None:
        """Wake every waiter to rerun its check, as when a hold begins."""
        for waiter in self._pending:
            waiter.wake.set()

    async def acquire(
        self,
        reserve: Callable[[], T | None],
        *,
        deadline: float,
        wait_s: float | None = None,
        check: Callable[[], float | None] | None = None,
    ) -> T:
        """Reserve immediately or wait within both the queue and request clocks.

        `wait_s` shortens the queue's wait limit for this waiter. `check` runs when the
        waiter enters the queue and each time it wakes. It raises to end the wait, or
        returns the router-clock time at which it must run again, or None.
        """
        now = self._clock()
        if now >= deadline:
            raise QueueExpired(at_deadline=True)
        if self._pending and len(self._pending) >= self.capacity:
            self.notify()
        if not self._pending:
            result = reserve()
            if result is not None:
                return result
        if len(self._pending) >= self.capacity:
            raise QueueFull
        recheck = self._recheck(check)
        limit = self.wait_s if wait_s is None else min(self.wait_s, wait_s)
        if limit <= 0:
            raise QueueExpired
        at_deadline = deadline <= now + limit
        expires = min(deadline, now + limit)
        waiter = _Waiter(reserve)
        self._pending[waiter] = None
        self.high_water = max(self.high_water, len(self._pending))
        try:
            while True:
                if waiter.error is not None:
                    raise waiter.error
                if waiter.granted:
                    return cast(T, waiter.result)
                # A due recheck runs its check; only the expiry timer ends the wait.
                rechecking = recheck is not None and recheck < expires
                bound = recheck if recheck is not None and rechecking else expires
                remaining = bound - self._clock()
                if remaining <= 0 and not rechecking:
                    raise QueueExpired(at_deadline=at_deadline)
                waiter.wake.clear()
                if remaining > 0:
                    try:
                        async with asyncio.timeout(remaining):
                            await waiter.wake.wait()
                    except TimeoutError as exc:
                        if not (rechecking or waiter.granted or waiter.error is not None):
                            raise QueueExpired(at_deadline=at_deadline) from exc
                if not (waiter.granted or waiter.error is not None):
                    recheck = self._recheck(check)
        finally:
            # A waiter that leaves without a grant passes its position to the next one.
            if waiter in self._pending:
                del self._pending[waiter]
                self.notify()

    def _recheck(self, check: Callable[[], float | None] | None) -> float | None:
        """Run `check`; a recheck time not after now waits for the next wake or expiry."""
        if check is None:
            return None
        recheck = check()
        return recheck if recheck is not None and recheck > self._clock() else None
