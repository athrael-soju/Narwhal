"""Shared scheduler data types."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

# Each engine leg failure increments one per-engine streak. Liveness counts
# failed sweeps. These names are also state keys and metric label values.
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
    # The requested cap feeds decode demand. output_len counts delivered tokens.
    wanted_len: int = 0
    # Monotonic ingress time for end-to-end TTFT projections.
    arrived_at: float | None = None

    @property
    def length(self) -> int:
        """Return total input and generated tokens."""
        return self.input_len + self.output_len


@dataclass
class Instance:
    """A stateless engine endpoint and its resident requests.

    Separate prefill and decode maps match the scheduler's phase-specific costs.
    """

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
