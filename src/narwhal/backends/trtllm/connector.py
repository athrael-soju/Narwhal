from __future__ import annotations

import secrets
from typing import Any, ClassVar

from ...engines.connector import KvConnector, PrefillResult

PARAMS = "disaggregated_params"
# The prompt as the context worker tokenized it; the generation leg continues from it.
PROMPT_IDS = "prompt_token_ids"


def _ints(values: object) -> bool:
    return isinstance(values, list) and all(type(value) is int and value >= 0 for value in values)


class TransceiverConnector(KvConnector):
    """The C++ cache transceiver: a context-only leg, then a generation-only leg."""

    param_key = PARAMS
    diagnostics_fields: ClassVar[tuple[str, ...]] = ("ctx_request_id",)
    # The generation leg returns the context leg's token and at least one more.
    min_output_tokens: ClassVar[int] = 2
    renewal_divisor: ClassVar[int] = 6

    def __init__(self, name: str, contract_name: str) -> None:
        self.name = name
        self.contract_name = contract_name

    def handoff_bound(self, lease_s: int) -> float:
        return float(lease_s - lease_s // self.renewal_divisor)

    def prefill_params(self) -> dict[str, Any]:
        return {
            PARAMS: {
                "request_type": "context_only",
                # The server takes a caller-chosen positive 63-bit ID for each handoff.
                "disagg_request_id": secrets.randbelow(2**63 - 1) + 1,
            }
        }

    def extract(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("prefill response must be an object")
        choices = payload.get("choices")
        if choices is None:
            # A decode leg rebuilds the parameters a prefill result holds.
            params = payload.get(PARAMS) or {}
            if not isinstance(params, dict):
                raise ValueError("handoff parameters must be an object")
            if params:
                self._validate(params)
            return params
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ValueError("prefill choices must contain objects")
        exported = choices[0].get(PARAMS)
        if exported is None:
            return {}
        if not isinstance(exported, dict):
            raise ValueError("handoff parameters must be an object")
        params = {**exported, PROMPT_IDS: payload.get(PROMPT_IDS)}
        self._validate(params)
        return params

    @staticmethod
    def _validate(params: dict[str, Any]) -> None:
        if type(params.get("ctx_request_id")) is not int:
            raise ValueError("handoff requires an integer ctx_request_id")
        if not _ints(params.get("first_gen_tokens")) or not params["first_gen_tokens"]:
            raise ValueError("handoff requires the context leg's first_gen_tokens")
        if not _ints(params.get(PROMPT_IDS)) or not params[PROMPT_IDS]:
            raise ValueError("handoff requires the context leg's prompt_token_ids")
        state = params.get("encoded_opaque_state")
        if state is not None and not isinstance(state, str):
            raise ValueError("encoded_opaque_state must be a string")

    def attach(self, body: dict[str, Any], params: dict[str, Any]) -> None:
        self._validate(params)
        exported = {key: value for key, value in params.items() if key != PROMPT_IDS}
        body[PARAMS] = {**exported, "request_type": "generation_only"}
        if "messages" in body:
            body[PROMPT_IDS] = params[PROMPT_IDS]
        else:
            body["prompt"] = params[PROMPT_IDS]

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
        leg.pop(PARAMS, None)
        leg.pop(PROMPT_IDS, None)
        if result is not None:
            if not isinstance(result, PrefillResult):
                raise ValueError("decode requires the typed result returned by prefill")
            if result.connector != self.name or result.endpoint != endpoint:
                raise ValueError("handoff connector or endpoint does not match decode")
            # A generation leg on its own context worker fails that worker's executor, so a
            # decode on the producer computes the prompt again.
            if result.producer_url != url:
                self.attach(leg, self.extract({PARAMS: result.parameters()}))
        return leg
