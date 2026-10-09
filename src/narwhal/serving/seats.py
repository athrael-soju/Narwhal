"""Per-engine prefill and decode seats from engine attestation and profiles."""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .router.routing import NarwhalRouter


class InputLengths:
    """Mean sized input length over a trailing window."""

    def __init__(self, window_s: float, clock: Callable[[], float]) -> None:
        self.window_s, self._clock = window_s, clock
        self._rows: deque[tuple[float, int]] = deque()
        self._total = 0

    def add(self, input_len: int) -> None:
        """Record one sized request at the current time."""
        self._rows.append((self._clock(), input_len))
        self._total += input_len

    def mean(self) -> float | None:
        """Return the mean input length inside the window, None without requests."""
        cutoff = self._clock() - self.window_s
        while self._rows and self._rows[0][0] < cutoff:
            self._total -= self._rows.popleft()[1]
        return self._total / len(self._rows) if self._rows else None


def decode_seats(router: NarwhalRouter, iid: str) -> int:
    """Return the lower of the attested sequence limit and profiled decode capacity, else 0."""
    profile = router.profiles.get(iid)
    limits = [
        limit
        for limit in (
            router.sequence_limits.get(iid),
            profile.decode_max_requests if profile is not None else None,
        )
        if limit is not None and limit > 0
    ]
    return min(limits) if limits else 0


def prefill_seats(router: NarwhalRouter, iid: str) -> int:
    """Return how many back-to-back profiled prefills at the mean input length fit `slo.ttft_s`.

    Returns 0, no limit, without a profile or a sized request in the window.
    """
    profile = router.profiles.get(iid)
    mean = router.input_lengths.mean()
    if profile is None or mean is None:
        return 0
    seconds = profile.prefill_time(max(1, round(mean)))
    if seconds <= 0:
        return 0
    return max(1, math.floor(router.cfg.slo.ttft_s / seconds))


def decode_seat_map(router: NarwhalRouter) -> dict[str, int]:
    """Return every engine's decode seats."""
    return {iid: decode_seats(router, iid) for iid in router.monitor.instances}


def snapshot(router: NarwhalRouter) -> dict[str, Any]:
    """Report each engine's seats and the inputs that set them."""
    mean = router.input_lengths.mean()
    return {
        "mean_input_len": None if mean is None else round(mean, 1),
        "engines": {
            iid: {
                "prefill": prefill_seats(router, iid),
                "decode": decode_seats(router, iid),
                "sequence_limit": router.sequence_limits.get(iid),
            }
            for iid in sorted(router.monitor.instances)
        },
    }
