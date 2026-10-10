"""KV handoff adapters for split serving."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal


class HandoffExpired(Exception):
    """The router can no longer trust a producer's KV ownership lease."""


@dataclass(frozen=True)
class PrefillResult:
    """Producer-owned KV descriptor with immutable serialized parameters.

    The original request lifecycle enforces handoff expiry.
    """

    connector: str
    producer_url: str
    endpoint: str
    producer_request_id: str | None
    descriptor_json: str = field(repr=False)
    exported_state: Literal["backend_owned_descriptor"] = field(
        default="backend_owned_descriptor", init=False
    )
    first_token: Literal["producer_output_discarded"] = field(
        default="producer_output_discarded", init=False
    )
    continuation: Literal["original_prompt"] = field(default="original_prompt", init=False)

    def parameters(self) -> dict[str, Any]:
        """Return an isolated copy of transport parameters."""
        params = json.loads(self.descriptor_json)
        if not isinstance(params, dict) or not params:
            raise ValueError("handoff parameters must be a nonempty object")
        return params


class KvHandoff(ABC):
    """One KV transport's handoff between a prefill leg and a decode leg."""

    name: str

    @abstractmethod
    def handoff_bound(self, lease_s: int) -> float:
        """Return the seconds after prefill within which decode may still consume the KV."""


class KvConnector(KvHandoff):
    """Prefill returns a descriptor that the decode leg carries."""

    # Same-engine decode must remove this client-supplied field.
    param_key: str = "kv_transfer_params"

    @abstractmethod
    def prefill_params(self) -> dict[str, Any]:
        """Return fields added to the prefill request."""

    @abstractmethod
    def extract(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Extract the handoff from a prefill response."""

    @abstractmethod
    def attach(self, body: dict[str, Any], params: dict[str, Any]) -> None:
        """Add the handoff to a decode request."""

    def prefill_result(
        self, payload: dict[str, Any], *, url: str, endpoint: str, request_id: str | None
    ) -> PrefillResult:
        """Bind a readable descriptor to the producer leg that returned it."""
        params = self.extract(payload)
        if not params:
            raise ValueError("missing handoff parameters")
        return PrefillResult(
            self.name, url, endpoint, request_id, json.dumps(params, allow_nan=False)
        )

    def decode_body(
        self, body: dict[str, Any], result: PrefillResult | None, *, url: str, endpoint: str
    ) -> dict[str, Any]:
        """Continue the original prompt, without exposing producer output.

        Same-worker decode and a None `result` omit transfer parameters.
        """
        leg = {**body, "stream": True}
        leg.pop(self.param_key, None)
        if result is not None:
            if not isinstance(result, PrefillResult):
                raise ValueError("decode requires the typed result returned by prefill")
            if result.connector != self.name or result.endpoint != endpoint:
                raise ValueError("handoff connector or endpoint does not match decode")
            params = self.extract({self.param_key: result.parameters()})
            if result.producer_url != url:
                self.attach(leg, params)
        return leg


class RendezvousConnector(KvHandoff):
    """Both legs carry one router-issued rendezvous and run concurrently."""

    # The engine never times out a decode leg whose prefill is lost.
    decode_wait_s: ClassVar[float]

    @abstractmethod
    def rendezvous(self, producer: Mapping[str, Any]) -> dict[str, Any]:
        """Return per-request fields binding both legs to the prefill engine `producer`."""

    @abstractmethod
    def prefill_body(self, body: dict[str, Any], rendezvous: dict[str, Any]) -> dict[str, Any]:
        """Build the prefill leg."""

    @abstractmethod
    def decode_body(self, body: dict[str, Any], rendezvous: dict[str, Any]) -> dict[str, Any]:
        """Build the decode leg."""


class NixlConnector(KvConnector):
    """Adapt vLLM's NixlConnector protocol."""

    name = "nixl"
    renewal_divisor: ClassVar[int] = 6

    def handoff_bound(self, lease_s: int) -> float:
        """Leave one lease renewal interval for decode to start consuming."""
        return float(lease_s - lease_s // self.renewal_divisor)

    def prefill_params(self) -> dict[str, Any]:
        """Request a remote decode handoff."""
        return {"kv_transfer_params": {"do_remote_decode": True}}

    def extract(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Return KV transfer parameters from a prefill response."""
        if not isinstance(payload, dict):
            raise ValueError("prefill response must be an object")
        choices = payload.get("choices") or []
        if not isinstance(choices, list) or (choices and not isinstance(choices[0], dict)):
            raise ValueError("prefill choices must contain objects")
        params = (
            (choices[0].get("kv_transfer_params") if choices else None)
            or payload.get("kv_transfer_params")
            or {}
        )
        if not isinstance(params, dict):
            raise ValueError("handoff parameters must be an object")
        if params:
            self._validate(params)
        return params

    @staticmethod
    def _validate(params: dict[str, Any]) -> None:
        """Check transport shape without asserting a cache-position convention."""
        engine = params.get("remote_engine_id")
        blocks = params.get("remote_block_ids")
        if not isinstance(engine, str) or not engine:
            raise ValueError("handoff requires remote_engine_id")

        def block_list(values: object) -> bool:
            return isinstance(values, list) and all(
                isinstance(block, int) and not isinstance(block, bool) and block >= 0
                for block in values
            )

        # Hybrid models export separate block lists per KV cache group.
        if not block_list(blocks) and not (
            isinstance(blocks, list) and all(block_list(group) for group in blocks)
        ):
            raise ValueError("handoff requires flat or grouped nonnegative remote_block_ids")

    def attach(self, body: dict[str, Any], params: dict[str, Any]) -> None:
        """Attach KV transfer parameters to a decode request."""
        self._validate(params)
        body["kv_transfer_params"] = params


# Each registered connector has passed the fleet checks for every engine pair.
_REGISTRY: dict[str, KvConnector] = {c.name: c for c in (NixlConnector(),)}


def lookup(name: str) -> KvConnector:
    """Return the registered connector named by the fleet config."""
    try:
        return _REGISTRY[name]
    except KeyError:
        raise ValueError(
            f"unknown connector {name!r}: known ones are {', '.join(sorted(_REGISTRY))}"
        ) from None
