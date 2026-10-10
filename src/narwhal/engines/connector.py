from __future__ import annotations

import json
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


class KvHandoff(ABC):
    name: str

    @abstractmethod
    def handoff_bound(self, lease_s: int) -> float: ...


class KvConnector(KvHandoff):
    # Same-engine decode must remove this client-supplied field.
    param_key: ClassVar[str]

    @abstractmethod
    def prefill_params(self) -> dict[str, Any]: ...

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
        self, body: dict[str, Any], result: PrefillResult | None, *, url: str, endpoint: str
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
    # The engine never times out a decode leg whose prefill is lost.
    decode_wait_s: ClassVar[float]

    @abstractmethod
    def rendezvous(self, producer: Mapping[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    def prefill_body(self, body: dict[str, Any], rendezvous: dict[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    def decode_body(self, body: dict[str, Any], rendezvous: dict[str, Any]) -> dict[str, Any]: ...
