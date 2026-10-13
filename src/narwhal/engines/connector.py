from __future__ import annotations

import json
import math
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal


class HandoffExpired(Exception): ...


@dataclass(frozen=True)
class PrefillResult:
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
        params = json.loads(self.descriptor_json)
        if not isinstance(params, dict) or not params:
            raise ValueError("handoff parameters must be a nonempty object")
        return params


# "sequential": the prefill leg returns a descriptor that the decode leg continues from.
# "concurrent": both legs run at once and meet at a rendezvous the router assigns.
LegOrder = Literal["sequential", "concurrent"]


class KvHandoff(ABC):
    name: str
    # The connector name an engine contract records for this handoff.
    contract_name: str
    leg_order: ClassVar[LegOrder]
    # Handoff fields that transfer diagnostics record for each pair.
    diagnostics_fields: ClassVar[tuple[str, ...]] = ()

    @property
    def concurrent(self) -> bool:
        return self.leg_order == "concurrent"

    @abstractmethod
    def handoff_bound(self, lease_s: int) -> float: ...

    def first_token_bound(self, limit: float | None) -> float | None:
        """Return the decode leg's first-token deadline under the caller's limit."""
        return limit

    def rendezvous(self, producer: Mapping[str, Any]) -> dict[str, Any] | None:
        """Assign the fields that pair a concurrent request's legs; None for sequential legs."""
        return None

    @abstractmethod
    def prefill_body(
        self, body: dict[str, Any], rendezvous: dict[str, Any] | None
    ) -> dict[str, Any]: ...

    @abstractmethod
    def decode_body(
        self,
        body: dict[str, Any],
        *,
        result: PrefillResult | None,
        rendezvous: dict[str, Any] | None,
        url: str,
        endpoint: str,
    ) -> dict[str, Any]: ...

    def prefill_result(
        self, payload: dict[str, Any], *, url: str, endpoint: str, request_id: str | None
    ) -> PrefillResult | None:
        return None


class KvConnector(KvHandoff):
    leg_order: ClassVar[LegOrder] = "sequential"

    # Same-engine decode must remove this client-supplied field.
    param_key: ClassVar[str]

    @abstractmethod
    def prefill_params(self) -> dict[str, Any]: ...

    def prefill_body(
        self, body: dict[str, Any], rendezvous: dict[str, Any] | None
    ) -> dict[str, Any]:
        return {**body, **self.prefill_params()}

    @abstractmethod
    def extract(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    def attach(self, body: dict[str, Any], params: dict[str, Any]) -> None: ...

    def prefill_result(
        self, payload: dict[str, Any], *, url: str, endpoint: str, request_id: str | None
    ) -> PrefillResult:
        params = self.extract(payload)
        if not params:
            raise ValueError("missing handoff parameters")
        return PrefillResult(
            self.name, url, endpoint, request_id, json.dumps(params, allow_nan=False)
        )

    def decode_body(
        self,
        body: dict[str, Any],
        *,
        result: PrefillResult | None,
        rendezvous: dict[str, Any] | None = None,
        url: str,
        endpoint: str,
    ) -> dict[str, Any]:
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
    leg_order: ClassVar[LegOrder] = "concurrent"
    # The engine never times out a decode leg whose prefill is lost.
    decode_wait_s: ClassVar[float]

    @abstractmethod
    def rendezvous(self, producer: Mapping[str, Any]) -> dict[str, Any]: ...

    def first_token_bound(self, limit: float | None) -> float:
        return min(limit or math.inf, self.decode_wait_s)
