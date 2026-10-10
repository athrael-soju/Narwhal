from __future__ import annotations

import secrets
from collections.abc import Mapping
from typing import Any, ClassVar

from ...engines.connector import RendezvousConnector

# Fields that pair one request's prefill and decode legs on the transfer backend.
BOOTSTRAP_FIELDS = ("bootstrap_host", "bootstrap_port", "bootstrap_room")


class SglangRendezvous(RendezvousConnector):
    # SGLang never times out a decode leg whose prefill is lost; prefill gives up after
    # SGLANG_DISAGGREGATION_BOOTSTRAP_TIMEOUT, which the launcher sets to this bound.
    decode_wait_s: ClassVar[float] = 30.0

    def handoff_bound(self, lease_s: int) -> float:
        return float(lease_s)

    def rendezvous(self, producer: Mapping[str, Any]) -> dict[str, Any]:
        bootstrap = producer.get("bootstrap")
        fields: dict[str, Any] = dict(bootstrap) if isinstance(bootstrap, Mapping) else {}
        # The transfer backend keys a handoff by room; rooms must be unique among live requests.
        fields["bootstrap_room"] = secrets.randbits(63)
        return fields

    def prefill_body(self, body: dict[str, Any], rendezvous: dict[str, Any]) -> dict[str, Any]:
        return {**body, **rendezvous}

    def decode_body(self, body: dict[str, Any], rendezvous: dict[str, Any]) -> dict[str, Any]:
        return {**_lengths(body), **rendezvous, "stream": True}


def _lengths(body: dict[str, Any]) -> dict[str, Any]:
    # SGLang ignores max_completion_tokens on completions; both routes honour max_tokens.
    if "max_completion_tokens" not in body:
        return dict(body)
    leg = dict(body)
    leg["max_tokens"] = leg.pop("max_completion_tokens")
    return leg


class MooncakeConnector(SglangRendezvous):
    name = "mooncake"
    contract_name = "mooncake"


class SglangNixlConnector(SglangRendezvous):
    name = "nixl"
    contract_name = "nixl"
