"""Bound raw-token history and acknowledge complete ASGI output groups."""

from __future__ import annotations

import math
import struct
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field

from ..engines.replay import ReplayEvent

_TOKEN = struct.Struct("!Q")
_MAX_TOKEN = (1 << 64) - 1
_BYTEARRAY_HEADER = sys.getsizeof(bytearray(1)) - 1
_BYTES_HEADER = sys.getsizeof(b"")
_LIST_HEADER = sys.getsizeof([])
_POINTER_BYTES = sys.getsizeof([None]) - _LIST_HEADER
_TOKEN_OBJECT_BYTES = sys.getsizeof(_MAX_TOKEN)


class HistoryCapacityError(ValueError):
    """A request or reservation cannot fit the configured history capacity."""


class HistoryLimitExceeded(ValueError):
    """Observed output exceeded its reserved token or wire storage."""


class HistoryBudget:
    """Reserve fixed history quotas synchronously within one router process."""

    __slots__ = ("high_water", "limit", "used")

    def __init__(self, limit: int) -> None:
        if type(limit) is not int or limit < 0:
            raise HistoryCapacityError("history budget must be a nonnegative integer")
        self.limit = limit
        self.used = 0
        self.high_water = 0

    def reserve(self, quota: int) -> HistoryReservation | None:
        """Reserve a complete request quota, or return None when capacity is occupied."""
        if type(quota) is not int or quota <= 0:
            raise HistoryCapacityError("history quota must be a positive integer")
        if quota > self.limit - self.used:
            return None
        reservation = HistoryReservation(self, quota)
        self.used += quota
        self.high_water = max(self.high_water, self.used)
        return reservation


class HistoryReservation:
    """Own one fixed budget allocation until request teardown releases it."""

    __slots__ = ("_budget", "_claimed", "bytes", "closed")

    def __init__(self, budget: HistoryBudget, quota: int) -> None:
        self._budget = budget
        self.bytes = quota
        self.closed = False
        self._claimed = False

    def close(self) -> None:
        """Return this allocation once; repeated teardown has no effect."""
        if not self.closed:
            self.closed = True
            self._budget.used -= self.bytes


@dataclass(frozen=True, slots=True)
class CommitGroup:
    """One ASGI body whose successful send commits its generated token frontier."""

    data: bytes = field(repr=False)
    frontier: int
    terminal: bool
    sequence: int


class ContinuationHistory:
    """Keep IDs and pending frames within one preallocated request reservation.

    The reservation covers packed IDs, one materialised replay list, pending
    wire storage, one incoming wire frame and one immutable ASGI body. Callers
    must retain at most one replay list and must finish the ASGI writer before
    closing history. Engine parsing and the original HTTP body have their own
    retention limits.
    """

    __slots__ = (
        "_finish_seen",
        "_ids",
        "_inflight",
        "_pending",
        "_pending_size",
        "_sequence",
        "closed",
        "committed_count",
        "first_committed_at",
        "first_observed_at",
        "last_committed_at",
        "last_observed_at",
        "max_tokens",
        "observed_count",
        "pending_capacity",
        "prompt_count",
        "reservation",
        "terminal_committed",
    )

    reservation: HistoryReservation
    prompt_count: int
    max_tokens: int
    pending_capacity: int
    committed_count: int
    observed_count: int
    terminal_committed: bool
    first_observed_at: float | None
    last_observed_at: float | None
    first_committed_at: float | None
    last_committed_at: float | None
    closed: bool
    _ids: bytearray
    _pending: bytearray
    _pending_size: int
    _finish_seen: bool
    _sequence: int
    _inflight: CommitGroup | None

    @classmethod
    def create(
        cls,
        prompt_ids: Sequence[int],
        max_tokens: int,
        reservation: HistoryReservation,
    ) -> ContinuationHistory:
        """Consume a reservation and allocate bounded ID and frame storage.

        Invalid inputs release the supplied reservation. A reservation already
        owned by another history cannot be reused or released by this call.
        """
        if reservation.closed or reservation._claimed:
            raise HistoryCapacityError("history reservation is unavailable")
        reservation._claimed = True
        try:
            if not prompt_ids or any(not _valid_token(token) for token in prompt_ids):
                raise HistoryCapacityError("prompt must contain unsigned 64-bit token IDs")
            if type(max_tokens) is not int or max_tokens <= 0:
                raise HistoryCapacityError("output limit must be a positive integer")
            self = cls.__new__(cls)
            token_capacity = len(prompt_ids) + max_tokens
            # List storage is reserved for one replay_prompt() result. The two
            # memoryviews and unpack tuple are bounded transient objects.
            fixed = (
                sys.getsizeof(self)
                + sys.getsizeof(reservation)
                + sys.getsizeof(CommitGroup(b"", 0, False, 0))
                + 2 * _BYTEARRAY_HEADER
                + 2 * _BYTES_HEADER
                + 2 * sys.getsizeof(memoryview(b""))
                + sys.getsizeof((0,))
                + _LIST_HEADER
                + token_capacity * (_TOKEN.size + _POINTER_BYTES + _TOKEN_OBJECT_BYTES)
                # State counters and timestamps remain bounded scalars.
                + len(cls.__slots__) * max(_TOKEN_OBJECT_BYTES, sys.getsizeof(0.0))
            )
            pending_capacity = (reservation.bytes - fixed) // 3
            if pending_capacity < 1:
                raise HistoryCapacityError("request cannot fit its history quota")
            self.reservation = reservation
            self.prompt_count = len(prompt_ids)
            self.max_tokens = max_tokens
            self.pending_capacity = pending_capacity
            self.committed_count = 0
            self.observed_count = 0
            self.terminal_committed = False
            self.first_observed_at = None
            self.last_observed_at = None
            self.first_committed_at = None
            self.last_committed_at = None
            self.closed = False
            self._ids = bytearray(_TOKEN.size * token_capacity)
            self._pending = bytearray(pending_capacity)
            self._pending_size = 0
            self._finish_seen = False
            self._sequence = 0
            self._inflight = None
            for index, token in enumerate(prompt_ids):
                _TOKEN.pack_into(self._ids, index * _TOKEN.size, token)
            return self
        except BaseException:
            reservation.close()
            raise

    @property
    def remaining(self) -> int:
        """Return the original output allowance after committed generated IDs."""
        return self.max_tokens - self.committed_count

    @property
    def pending_size(self) -> int:
        """Return retained frame bytes that have not been acknowledged by ASGI."""
        return self._pending_size

    @property
    def inflight(self) -> CommitGroup | None:
        """Return the sole group awaiting an ASGI send acknowledgement."""
        return self._inflight

    def append(self, event: ReplayEvent, wire: bytes, observed_at: float) -> CommitGroup | None:
        """Retain a validated event and offer a group only at a qualified boundary."""
        self._require_open()
        if self._inflight is not None:
            raise RuntimeError("continuation group still awaits acknowledgement")
        if self.terminal_committed:
            raise RuntimeError("continuation output is already terminal")
        if not isinstance(wire, bytes) or not wire:
            raise ValueError("continuation frame must contain serialised bytes")
        _validate_time(observed_at)
        ids = event.generated_ids
        if any(not _valid_token(token) for token in ids):
            raise ValueError("invalid continuation token identity")
        if event.kind == "completion":
            if self._finish_seen:
                raise ValueError("continuation completion follows a finish reason")
            if not isinstance(event.text, str):
                raise ValueError("continuation completion text must be a string")
        elif event.kind == "done":
            if not self._finish_seen or ids:
                raise ValueError("continuation terminator lacks a finished choice")
        elif event.kind != "usage" or ids:
            raise ValueError("invalid continuation metadata")
        if len(ids) > self.max_tokens - self.observed_count:
            raise HistoryLimitExceeded("continuation output token limit exceeded")
        if len(wire) > self.pending_capacity - self._pending_size:
            raise HistoryLimitExceeded("continuation pending byte limit exceeded")

        offset = (self.prompt_count + self.observed_count) * _TOKEN.size
        for token in ids:
            _TOKEN.pack_into(self._ids, offset, token)
            offset += _TOKEN.size
        self.observed_count += len(ids)
        if ids:
            if self.first_observed_at is None:
                self.first_observed_at = observed_at
            self.last_observed_at = observed_at
        end = self._pending_size + len(wire)
        memoryview(self._pending)[self._pending_size : end] = wire
        self._pending_size = end
        if event.finish_reason is not None:
            self._finish_seen = True

        terminal = event.kind == "done"
        safe = (
            event.kind == "completion"
            and len(ids) == 1
            and bool(event.text)
            and event.finish_reason is None
        )
        if not terminal and not safe:
            return None
        self._sequence += 1
        group = CommitGroup(
            bytes(memoryview(self._pending)[:end]),
            self.observed_count,
            terminal,
            self._sequence,
        )
        self._inflight = group
        return group

    def commit(self, group: CommitGroup, accepted_at: float) -> None:
        """Advance the exact offered frontier after its ASGI send returns."""
        self._require_open()
        if group is not self._inflight:
            raise RuntimeError("continuation acknowledgement does not match the pending group")
        _validate_time(accepted_at)
        if group.frontier > self.committed_count:
            if self.first_committed_at is None:
                self.first_committed_at = accepted_at
            self.last_committed_at = accepted_at
        self.committed_count = group.frontier
        self.terminal_committed = group.terminal
        self._pending_size = 0
        self._inflight = None

    def discard_pending(self) -> None:
        """Drop unacknowledged output while preserving the original committed prefix."""
        self._require_open()
        if self.terminal_committed:
            raise RuntimeError("terminal continuation cannot discard output")
        self.observed_count = self.committed_count
        self._pending_size = 0
        self._finish_seen = False
        self._inflight = None

    def replay_prompt(self) -> list[int]:
        """Materialise the original prompt and committed IDs within the reserved copy quota."""
        self._require_open()
        count = self.prompt_count + self.committed_count
        result = [0] * count
        for index in range(count):
            result[index] = _TOKEN.unpack_from(self._ids, index * _TOKEN.size)[0]
        return result

    def close(self) -> None:
        """Clear owned buffers and return the reservation once after writer teardown."""
        if self.closed:
            return
        self.closed = True
        self._inflight = None
        self._ids = bytearray()
        self._pending = bytearray()
        self._pending_size = 0
        self.reservation.close()

    def _require_open(self) -> None:
        if self.closed or self.reservation.closed:
            raise RuntimeError("continuation history is closed")


def _valid_token(token: object) -> bool:
    return type(token) is int and 0 <= token <= _MAX_TOKEN


def _validate_time(value: float) -> None:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("continuation timestamp must be finite")
