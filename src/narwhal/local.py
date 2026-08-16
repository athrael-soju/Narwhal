"""The per-instance local scheduler: migration queue and chunked prefill (Arrow §5.4)."""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field

from .profiler import Profile


@dataclass
class _Prefilling:
    """A prefill request part-way through its chunks."""

    rid: str
    input_len: int
    done: int = 0

    @property
    def remaining(self) -> int:
        return self.input_len - self.done


@dataclass
class Iteration:
    """What one engine iteration did, and what it cost."""

    seconds: float
    decoded: list[str] = field(default_factory=list)
    prefilled: list[str] = field(default_factory=list)
    completed_prefill: list[str] = field(default_factory=list)


class LocalScheduler:
    """One instance's batch construction.

    `batch_tokens` is Arrow §5.4's given batch size and bounds membership, not just
    price. `chunk_max` caps a single prefill chunk.
    """

    def __init__(self, batch_tokens: int = 16384, chunk_max: int = 2048) -> None:
        self.batch_tokens = batch_tokens
        self.chunk_max = chunk_max
        self.migration: deque[tuple[str, float]] = deque()
        self.prefill_queue: deque[_Prefilling] = deque()
        self.decode_ready: list[str] = []

    # -- admission ------------------------------------------------------

    def admit_prefill(self, rid: str, input_len: int, cached: int = 0) -> None:
        """Queue a prefill; chunks are cut at execute time (Arrow §5.4).

        `cached` tokens are already computed on this engine (prefix reuse,
        cooperative reuse): the pass skips to the first uncomputed token. The last token
        always computes - o1 is sampled from it.
        """
        start = min(cached, max(0, input_len - 1))
        self.prefill_queue.append(_Prefilling(rid=rid, input_len=input_len, done=start))

    def admit_decode(self, rid: str, ready_at: float | None = None) -> None:
        """`ready_at=None` means no migration is required, so skip the queue."""
        if ready_at is None:
            self.decode_ready.append(rid)
            return
        self.migration.append((rid, ready_at))

    def release_migrations(self, now: float) -> list[str]:
        """FCFS: a later arrival never overtakes an earlier one."""
        released = []
        while self.migration and self.migration[0][1] <= now:
            rid, _ = self.migration.popleft()
            self.decode_ready.append(rid)
            released.append(rid)
        return released

    def drop(self, rid: str) -> None:
        """Remove `rid` wherever it waits; a failed request must not haunt the batch."""
        if rid in self.decode_ready:
            self.decode_ready.remove(rid)
        self.prefill_queue = deque(p for p in self.prefill_queue if p.rid != rid)
        self.migration = deque(m for m in self.migration if m[0] != rid)

    # -- one iteration --------------------------------------------------

    def step(self, profile: Profile, decode_tokens: Mapping[str, int]) -> Iteration:
        """Build the running batch and price it.

        `decode_tokens` maps each ready request to its current token count.
        Decode is admitted FCFS while it fits, then chunked prefill fills the
        remaining space, one chunk per queued request.
        """
        admitted: list[str] = []
        used = 0
        for rid in self.decode_ready:
            n = max(0, int(decode_tokens.get(rid, 0)))
            # `admitted and`: a request larger than the batch must still run.
            if admitted and used + n > self.batch_tokens:
                break
            admitted.append(rid)
            used += n

        seconds = profile.token_interval(min(used, self.batch_tokens)) if admitted else 0.0

        prefilled: list[str] = []
        completed: list[str] = []
        remaining_budget = max(0, self.batch_tokens - used)
        idx = 0
        while remaining_budget > 0 and idx < len(self.prefill_queue):
            head = self.prefill_queue[idx]
            chunk = min(head.remaining, self.chunk_max, remaining_budget)
            if chunk > 0:
                seconds += profile.prefill_time(head.done + chunk) - profile.prefill_time(head.done)
                head.done += chunk
                remaining_budget -= chunk
                prefilled.append(head.rid)
                if head.remaining == 0:
                    completed.append(head.rid)
            idx += 1
        if completed:
            self.prefill_queue = deque(p for p in self.prefill_queue if p.remaining > 0)

        return Iteration(
            seconds=max(seconds, 0.0),
            decoded=admitted,
            prefilled=prefilled,
            completed_prefill=completed,
        )
