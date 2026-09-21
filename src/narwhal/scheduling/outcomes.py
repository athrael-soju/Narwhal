"""Bounded time buckets for request SLO diagnostics."""

from __future__ import annotations

import math
from collections.abc import Callable


class OutcomeWindow:
    """Accumulate outcomes in time buckets with a fixed retained horizon."""

    def __init__(self, clock: Callable[[], float], bucket_s: float, retained_s: float) -> None:
        self._clock = clock
        # Fixed-width buckets bound storage independently of request rate.
        # Retain the complete bucket containing the horizon boundary.
        if not (math.isfinite(bucket_s) and bucket_s > 0):
            raise ValueError(f"outcome_bucket_s must be positive, got {bucket_s}")
        if not (math.isfinite(retained_s) and retained_s > 0):
            raise ValueError(f"outcome_retained_s must be positive, got {retained_s}")
        self.bucket_s = bucket_s
        self.retained_s = retained_s
        # Bucket index (floor(t / bucket_s)) -> [ttft_ok, tpot_ok, total].
        self.buckets: dict[int, list[int]] = {}
        self._head = -1  # highest bucket index noted so far
        self.pruned_buckets = 0
        self.pruned_outcomes = 0

    def note_outcome(self, ttft_ok: bool, tpot_ok: bool, *, at: float | None = None) -> None:
        """Record one terminal request's SLO outcome into its time bucket.

        Pruning runs only when a new bucket advances the head.
        """
        now = self._clock() if at is None else at
        index = math.floor(now / self.bucket_s)
        bucket = self.buckets.get(index)
        if bucket is None:
            bucket = self.buckets[index] = [0, 0, 0]
        if index > self._head:
            self._head = index
            self._prune_outcomes()
        bucket[0] += ttft_ok
        bucket[1] += tpot_ok
        bucket[2] += 1

    def _prune_outcomes(self) -> None:
        """Drop buckets outside the retained horizon when the head advances."""
        cutoff = self._head - int(self.retained_s / self.bucket_s)
        for index in [i for i in self.buckets if i < cutoff]:
            self.pruned_buckets += 1
            self.pruned_outcomes += self.buckets.pop(index)[2]

    def outcome_counts(self, since_s: float) -> tuple[int, int, int]:
        """Return (ttft_ok, tpot_ok, total) recorded at or after `since_s`.

        Evidence granularity is one bucket: the bucket containing `since_s`
        counts whole, so a window narrower than a bucket approximates to one
        bucket. Counting scans the bounded bucket map.
        """
        start = math.floor(since_s / self.bucket_s)
        ttft_ok = tpot_ok = total = 0
        for index, bucket in self.buckets.items():
            if index >= start:
                ttft_ok += bucket[0]
                tpot_ok += bucket[1]
                total += bucket[2]
        return ttft_ok, tpot_ok, total

    def outcome_summary(self) -> dict[str, int | float]:
        """Return retained outcome counts, time coverage and prune totals."""
        outcomes = sum(bucket[2] for bucket in self.buckets.values())
        covered_s = 0.0
        if self.buckets:
            oldest = min(self.buckets) * self.bucket_s
            covered_s = min(self.retained_s, max(0.0, self._clock() - oldest))
        return {
            "bucket_s": self.bucket_s,
            "retained_s": self.retained_s,
            "covered_s": covered_s,
            "buckets": len(self.buckets),
            "outcomes": outcomes,
            "pruned_buckets": self.pruned_buckets,
            "pruned_outcomes": self.pruned_outcomes,
        }
