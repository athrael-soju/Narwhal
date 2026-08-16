"""The vocabulary Arrow's algorithms are written in.

Phase is a property of a request, not an instance (Arrow §5.2). An instance's `role`
is only the pool label the scheduler draws from, so flipping is a relabel.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Phase(StrEnum):
    """A property of a request, never of an instance."""

    PREFILL = "prefill"
    DECODE = "decode"


class Role(StrEnum):
    """Which pool the scheduler draws this instance from. A label, not a capability."""

    PREFILL = "prefill"
    DECODE = "decode"


@dataclass
class Request:
    """One request, split into two independently schedulable sub-requests (Arrow §5.2).

    `prefill_instance` is read by Algorithm 1's first branch.
    """

    rid: str
    input_len: int
    phase: Phase = Phase.PREFILL
    prefill_instance: str | None = None
    output_len: int = 0
    # Times the queued prefill leg has been re-placed. Capped low by the
    # caller: a leg that will not start anywhere must not ping-pong.
    replaced: int = 0
    # Hash of the prompt head, set only when a prefix arm is on (the affinity
    # ablation and the cooperative term): identity for "these requests share
    # a prefix", nothing more.
    prefix_key: int | None = None
    # Tokens of shared prefix the discount may claim. The router
    # proves identity only over the span it hashes, so it claims only what
    # that span covers; a trace that knows its head sets the real span.
    prefix_len: int | None = None
    # Which tenant admitted this request; the batched gate orders the
    # window by the tenant's share so heavier classes place first, and the
    # journal row carries it so the accounting stays per customer.
    tenant: str = "anonymous"

    @property
    def length(self) -> int:
        """`L(r)` in the cost functions of §5.3."""
        return self.input_len + self.output_len


@dataclass
class Instance:
    """A stateless engine endpoint.

    `prefill` and `decode` are kept separate because every cost function in
    §5.3 and Arrow §5.5 reads one or the other, never the union.
    """

    iid: str
    url: str
    role: Role = Role.DECODE
    prefill: dict[str, Request] = field(default_factory=dict)
    decode: dict[str, Request] = field(default_factory=dict)

    def decode_tokens(self) -> int:
        """`sum(L(rd) for rd in D)`."""
        return sum(r.length for r in self.decode.values())

    def prefill_tokens(self) -> int:
        """`sum(L(rp) for rp in P)`."""
        return sum(r.length for r in self.prefill.values())
