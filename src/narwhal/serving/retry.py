"""Transient failure classification, full jitter, and a router-wide retry quota."""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction

import httpx

from ..engines.client import EngineError
from ..engines.connector import HandoffExpired


def transient(exc: BaseException) -> bool:
    """Recognize retryable transport and service failures before output starts."""
    if isinstance(exc, HandoffExpired):
        return True
    if isinstance(exc, EngineError):
        return exc.status in (408, 429, 500, 502, 503, 504)
    # Local pool timeouts indicate router saturation.
    if isinstance(exc, httpx.PoolTimeout):
        return False
    return isinstance(exc, httpx.NetworkError | httpx.TimeoutException | httpx.RemoteProtocolError)


@dataclass(frozen=True)
class RetryPolicy:
    """Bound complete prefill/decode attempts for each original request."""

    max_attempts: int = 1
    base_delay_s: float = 0.1
    max_delay_s: float = 1.0

    def delay(self, attempts: int, random_value: Callable[[], float] = random.random) -> float:
        """Return full jitter after the given one-based failed attempt."""
        return random_value() * min(self.max_delay_s, self.base_delay_s * 2 ** (attempts - 1))


class RetryBudget:
    """Bound retry work to initial credits plus credits earned by completions.

    Each retry consumes one credit. Successful original requests refill the bucket.
    """

    def __init__(self, capacity: int, replenish: float) -> None:
        self.capacity = capacity
        self.replenish = Fraction(str(replenish))
        self._available = Fraction(capacity)
        self.spent = 0
        self.denied = 0

    @property
    def available(self) -> float:
        """Expose credit without rounding the accounting that gates retries."""
        return float(self._available)

    def acquire(self) -> bool:
        """Consume one retry credit without waiting or borrowing."""
        if self._available < 1:
            self.denied += 1
            return False
        self._available -= 1
        self.spent += 1
        return True

    def succeeded(self) -> None:
        """Credit one successful original request, capped at bucket capacity."""
        self._available = min(Fraction(self.capacity), self._available + self.replenish)


def leg_failure_reason(exc: BaseException) -> str | None:
    """Classify failures for quarantine, excluding invalid client requests."""
    if isinstance(exc, EngineError):
        status = exc.status
        if 400 <= status < 500 and status not in (408, 429):
            return None
        return "overloaded" if status in (408, 429) else "engine_error"
    if isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout):
        return "engine_dead"
    if isinstance(exc, httpx.PoolTimeout):
        # Preserve engine availability after a local connection-pool timeout.
        return "local_pool"
    if isinstance(exc, httpx.TimeoutException | TimeoutError):
        return "timeout"
    return "engine_dead"
