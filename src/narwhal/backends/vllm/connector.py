from __future__ import annotations

from typing import Any, ClassVar

from ...engines.connector import KvConnector


class NixlConnector(KvConnector):
    name = "nixl"
    contract_name = "NixlConnector"
    param_key = "kv_transfer_params"
    renewal_divisor: ClassVar[int] = 6

    def handoff_bound(self, lease_s: int) -> float:
        return float(lease_s - lease_s // self.renewal_divisor)

    def prefill_params(self) -> dict[str, Any]:
        return {"kv_transfer_params": {"do_remote_decode": True}}

    def extract(self, payload: dict[str, Any]) -> dict[str, Any]:
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
        self._validate(params)
        body["kv_transfer_params"] = params
