"""Shared scheduler data types."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# Breaker class names, also used as state keys and metric label values.
# Liveness counts failed sweeps.
LEG_CONNECTION = "connection"
LEG_TIMEOUT = "timeout"
LEG_OVERLOAD = "overload"
LEG_INFERENCE_STATUS = "inference_status"
LEG_KV_HANDOFF = "kv_handoff"
LEG_STREAM = "stream"
LIVENESS = "liveness"
# Engine-leg failure classes.
LEG_CLASSES = (
    LEG_CONNECTION,
    LEG_TIMEOUT,
    LEG_OVERLOAD,
    LEG_INFERENCE_STATUS,
    LEG_KV_HANDOFF,
    LEG_STREAM,
)
# Every streak class, liveness included.
FAILURE_CLASSES = (*LEG_CLASSES, LIVENESS)


class Phase(StrEnum):
    """Current processing phase of a request."""

    PREFILL = "prefill"
    DECODE = "decode"


class Role(StrEnum):
    """Scheduler pool assigned to an engine."""

    PREFILL = "prefill"
    DECODE = "decode"


@dataclass
class Request:
    """Request state shared by the prefill and decode schedulers."""

    rid: str
    input_len: int
    phase: Phase = Phase.PREFILL
    prefill_instance: str | None = None
    output_len: int = 0
    # Requested output cap; output_len counts delivered tokens.
    wanted_len: int = 0
    # Monotonic ingress time for end-to-end TTFT projections.
    arrived_at: float | None = None
    # Cached prompt tokens per engine, rechecked at each prefill placement.
    cached_tokens: dict[str, int] = field(default_factory=dict)
    # Residency sequence behind each engine's evidence.
    cache_sequences: dict[str, int] = field(default_factory=dict)
    # Matched prompt block identities by block size, for the placement recheck.
    cache_identities: dict[int, list[bytes]] = field(default_factory=dict)
    # Router clock time of the last sizing or recheck of the cache evidence.
    cache_checked_at: float | None = None
    # Journal record of the cache-priced placement.
    cache_placement: dict[str, Any] | None = None
    # Offered-demand cohort, repriced when cache evidence changes.
    demand_arrival: Any = None

    @property
    def length(self) -> int:
        """Return total input and generated tokens."""
        return self.input_len + self.output_len


@dataclass
class Instance:
    """A stateless engine endpoint and its resident requests."""

    iid: str
    url: str
    role: Role = Role.DECODE
    prefill: dict[str, Request] = field(default_factory=dict)
    decode: dict[str, Request] = field(default_factory=dict)

    def decode_tokens(self) -> int:
        """Return tokens resident in decode."""
        return sum(r.length for r in self.decode.values())

    def prefill_tokens(self) -> int:
        """Return tokens resident in prefill."""
        return sum(r.length for r in self.prefill.values())
