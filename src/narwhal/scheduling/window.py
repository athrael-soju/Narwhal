"""Rate-independent demand history with counted, conservative overflow cohorts."""

from __future__ import annotations

import math
from collections.abc import Callable, Hashable, Iterator
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T", bound=Hashable)


@dataclass
class Cohort(Generic[T]):
    """Identical observations, or a conservative envelope after shape overflow."""

    value: T
    first: float
    last: float
    count: int = 1
    overflow: bool = False
    last_certain: bool = True


class DemandWindow(Generic[T]):
    """Retain at most `max_shapes + 1` cohorts per fixed time bucket.

    Overflow preserves every count and merges values through a caller-supplied
    upper envelope. A boundary cohort contributes its whole count to demand;
    safety evidence uses only observations guaranteed to follow the cutoff.
    Recording prunes history even when the control loop is unavailable.
    """

    def __init__(
        self,
        clock: Callable[[], float],
        *,
        retained_s: float,
        bucket_s: float,
        merge: Callable[[T, T], T],
        max_shapes: int = 128,
    ) -> None:
        if not (math.isfinite(retained_s) and retained_s > 0):
            raise ValueError("retained_s must be finite and positive")
        if not (math.isfinite(bucket_s) and bucket_s > 0):
            raise ValueError("bucket_s must be finite and positive")
        if max_shapes < 1:
            raise ValueError("max_shapes must be positive")
        self._clock = clock
        self.retained_s = retained_s
        self.bucket_s = bucket_s
        self.max_shapes = max_shapes
        self._merge = merge
        self._buckets: dict[int, dict[T, Cohort[T]]] = {}
        self._overflow: dict[int, Cohort[T]] = {}
        self._head = math.floor(clock() / bucket_s)

    def prune(self, now: float | None = None) -> None:
        """Advance retention without relying on a successful planning pass."""
        index = math.floor((self._clock() if now is None else now) / self.bucket_s)
        if index <= self._head:
            return
        self._head = index
        cutoff = index - math.ceil(self.retained_s / self.bucket_s)
        for old in [key for key in self._buckets if key < cutoff]:
            del self._buckets[old]
            self._overflow.pop(old, None)

    def add(self, value: T, *, at: float | None = None) -> Cohort[T] | None:
        """Count an observation using bounded storage, including excess shapes."""
        now = self._clock()
        seen = now if at is None else at
        self.prune(max(now, seen))
        index = math.floor(seen / self.bucket_s)
        if index < self._head - math.ceil(self.retained_s / self.bucket_s):
            return None
        bucket = self._buckets.setdefault(index, {})
        row = bucket.get(value)
        if row is None:
            if len(bucket) < self.max_shapes:
                row = bucket[value] = Cohort(value, seen, seen)
                return row
            row = self._overflow.get(index)
            if row is None:
                row = self._overflow[index] = Cohort(value, seen, seen, overflow=True)
                return row
            row.value = self._merge(row.value, value)
        row.count += 1
        row.first = min(row.first, seen)
        if seen >= row.last:
            row.last_certain = True
        row.last = max(row.last, seen)
        return row

    def replace(self, row: Cohort[T] | None, value: T, *, at: float) -> None:
        """Reprice one retained observation without adding demand or refreshing age.

        Only active request owners retain cohort references. A removed boundary
        observation invalidates the cohort's last-timestamp evidence; its old
        upper envelope still conservatively bounds remaining work.
        """
        self.prune()
        index = math.floor(at / self.bucket_s)
        bucket = self._buckets.get(index)
        if row is None or bucket is None:
            return
        present = self._overflow.get(index) is row if row.overflow else bucket.get(row.value) is row
        if not present or (not row.overflow and row.value == value):
            return
        row.count -= 1
        if at == row.last:
            row.last_certain = False
        if not row.count:
            if row.overflow:
                del self._overflow[index]
            else:
                del bucket[row.value]
        self.add(value, at=at)

    def rows(self, since: float | None = None) -> Iterator[Cohort[T]]:
        """Read bounded cohorts, including a whole cohort across a cutoff."""
        self.prune()
        cutoff = self._clock() - self.retained_s if since is None else since
        for index, bucket in self._buckets.items():
            for row in bucket.values():
                if row.last >= cutoff:
                    yield row
            overflow = self._overflow.get(index)
            if overflow is not None and overflow.last >= cutoff:
                yield overflow

    def count(self, since: float | None = None) -> int:
        """Count observations, including all coalesced shapes."""
        return sum(row.count for row in self.rows(since))

    def evidence(self, since: float) -> tuple[int, float | None]:
        """Return guaranteed post-cutoff count and oldest guaranteed timestamp.

        A cohort straddling the cutoff contributes one observation when its
        last timestamp is certain. Later cohorts contribute their full count.
        """
        count = 0
        oldest: float | None = None
        for row in self.rows(since):
            whole = row.first >= since
            if not whole and not row.last_certain:
                continue
            count += row.count if whole else 1
            seen = row.first if whole else row.last
            oldest = seen if oldest is None else min(oldest, seen)
        return count, oldest

    def clear(self) -> None:
        """Discard observations without changing the window configuration."""
        self._buckets.clear()
        self._overflow.clear()

    def summary(self) -> dict[str, int | float]:
        """Expose the storage bound and approximation currently in the window."""
        rows = list(self.rows())
        return {
            "bucket_s": self.bucket_s,
            "retained_s": self.retained_s,
            "cell_limit": (math.ceil(self.retained_s / self.bucket_s) + 1) * (self.max_shapes + 1),
            "cells": sum(len(bucket) for bucket in self._buckets.values()) + len(self._overflow),
            "observations": sum(row.count for row in rows),
            "overflow_observations": sum(row.count for row in rows if row.overflow),
        }


def weighted_median(values: list[tuple[float, int]]) -> float:
    """Compute the ordinary median of counted values without expanding them."""
    count = sum(weight for _, weight in values)
    if not count:
        raise ValueError("median requires observations")
    lower, upper = (count - 1) // 2, count // 2
    consumed = 0
    low = 0.0
    for value, weight in sorted(values):
        consumed += weight
        if consumed - weight <= lower < consumed:
            low = value
        if consumed > upper:
            return (low + value) / 2
    raise AssertionError("invalid observation weights")
