"""The KV-transport contract, named.

Arrow §5.2's requirement is a stateless engine facing any peer over whichever KV
transport the build carries. The mechanics differ per connector — NixlConnector
wants the prefill leg forced to one token with `do_remote_decode`, and answers
with a `kv_transfer_params` dict — but the router's protocol around it is
fixed: force one token out of prefill, ask the connector to read its handoff
out of the response, and pass that handoff with the decode leg, verbatim.

A connector is an instance behind this module's tiny ABC, named in the fleet
config's `connector` key; the check gates (`narwhal-check`) are the
acceptance mechanism — a connector is supported when every ordered pair of
engines passes `produce` and `consume` against reference hardware. Today the
registry holds exactly one and the wire is bit-identical to what predates
this module: the seam is the PR, not a behaviour change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class KvConnector(ABC):
    """The three narrow choices a KV transport makes around the worker protocol."""

    name: str
    # The body key the transport's handoff rides under on both legs, so an
    # uncrossed decode knows what stale key to strip from a client's body.
    # Part of the contract: engine.py pops it on every uncrossed decode.
    param_key: str = "kv_transfer_params"

    @abstractmethod
    def prefill_params(self) -> dict[str, Any]:
        """What the prefill leg asks for: merged over the client's body."""

    @abstractmethod
    def extract(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The handoff, read out of the prefill response ({} means none)."""

    @abstractmethod
    def attach(self, body: dict[str, Any], params: dict[str, Any]) -> None:
        """Put the handoff on the decode leg, in place."""


class NixlConnector(KvConnector):
    """vLLM's NixlConnector over `kv_transfer_params`, the reference transport."""

    name = "nixl"

    def prefill_params(self) -> dict[str, Any]:
        """Offer the remote decode: NIXL's one-token prefill handshake."""
        return {"kv_transfer_params": {"do_remote_decode": True}}

    def extract(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Read the handoff from the choice, with a framed answer as fallback."""
        params = (
            payload.get("choices", [{}])[0].get("kv_transfer_params")
            or payload.get("kv_transfer_params")
            or {}
        )
        return dict(params)

    def attach(self, body: dict[str, Any], params: dict[str, Any]) -> None:
        """The handoff rides on the decode leg under the params key."""
        body["kv_transfer_params"] = params


# The registry is small on purpose: a name joins it only with engines the
# gates have cleared, not with code that merely exists.
_REGISTRY: dict[str, KvConnector] = {c.name: c for c in (NixlConnector(),)}


def lookup(name: str) -> KvConnector:
    """The connector the config names, or a refusal naming it back."""
    try:
        return _REGISTRY[name]
    except KeyError:
        raise ValueError(
            f"unknown connector {name!r}: known ones are {', '.join(sorted(_REGISTRY))}"
        ) from None
