from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from typing import Any, ClassVar

from ...engines.connector import (
    EngineCalls,
    HandoffPhase,
    HandoffStepError,
    KvConnector,
    PrefillResult,
)

SEAT_FIELD = "id_slot"
PROPS_TIMEOUT_S = 2.0
# Saved files each producer slot cycles through. A consumer that restores a file its producer
# has since overwritten holds another prompt's state, and the server then computes the tokens
# that differ, so reuse costs time and never a wrong reply.
FILES_PER_SLOT = 2


class SlotFileConnector(KvConnector):
    """Hand KV state over as a slot file: the producer saves its slot and the consumer restores it.

    Every engine of a fleet starts with the same --slot-save-path directory.
    """

    name = "slot-file"
    contract_name = "slot-file"
    param_key: ClassVar[str] = SEAT_FIELD
    diagnostics_fields: ClassVar[tuple[str, ...]] = ("filename",)
    staged: ClassVar[bool] = True

    def __init__(self) -> None:
        # Free slots per engine, most recently freed last, so a decode on the producer
        # usually takes the slot its prefill left warm.
        self._free: dict[str, list[int]] = {}
        # Legs waiting for a slot on each engine, first come first served.
        self._waiting: dict[str, list[asyncio.Future[int]]] = {}
        self._saves: dict[tuple[str, int], int] = {}

    def handoff_bound(self, lease_s: int) -> float:
        return float(lease_s)

    def prefill_params(self) -> dict[str, Any]:
        return {"cache_prompt": True}

    def extract(self, payload: dict[str, Any]) -> dict[str, Any]:
        params = payload.get(self.param_key)
        return dict(params) if isinstance(params, dict) else {}

    def attach(self, body: dict[str, Any], params: dict[str, Any]) -> None:
        return None

    def prefill_result(
        self, payload: dict[str, Any], *, url: str, endpoint: str, request_id: str | None
    ) -> PrefillResult:
        # `export` names the saved file once the producer has written it.
        return PrefillResult(self.name, url, endpoint, request_id, json.dumps({"filename": ""}))

    def decode_body(
        self,
        body: dict[str, Any],
        *,
        result: PrefillResult | None,
        rendezvous: dict[str, Any] | None = None,
        url: str,
        endpoint: str,
    ) -> dict[str, Any]:
        if result is not None and (result.connector != self.name or result.endpoint != endpoint):
            raise ValueError("handoff connector or endpoint does not match decode")
        leg = {**body, "stream": True, "cache_prompt": True}
        leg.pop(self.param_key, None)
        return leg

    async def claim(self, calls: EngineCalls, url: str, phase: HandoffPhase) -> dict[str, Any]:
        free = self._free.get(url)
        if free is None:
            props = await calls.get(url, "/props", timeout_s=PROPS_TIMEOUT_S)
            slots = props.get("total_slots")
            if type(slots) is not int or slots < 1:
                raise HandoffStepError(502, "/props reports no total_slots")
            free = self._free.setdefault(url, list(range(slots - 1, -1, -1)))
        if free:
            return {SEAT_FIELD: free.pop()}
        # The leg's deadline cancels the wait.
        turn: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        waiting = self._waiting.setdefault(url, [])
        waiting.append(turn)
        try:
            return {SEAT_FIELD: await turn}
        except asyncio.CancelledError:
            if turn in waiting:
                waiting.remove(turn)
            elif not turn.cancelled():
                self._hand_on(url, turn.result())
            raise

    def release(self, url: str, phase: HandoffPhase, seat: Mapping[str, Any]) -> None:
        slot = seat.get(SEAT_FIELD)
        free = self._free.get(url)
        if free is not None and type(slot) is int and slot not in free:
            self._hand_on(url, slot)
        return None

    def _hand_on(self, url: str, slot: int) -> None:
        waiting = self._waiting.get(url)
        if waiting:
            waiting.pop(0).set_result(slot)
        else:
            self._free[url].append(slot)

    async def export(
        self,
        calls: EngineCalls,
        url: str,
        seat: Mapping[str, Any],
        result: PrefillResult,
        *,
        timeout_s: float,
    ) -> PrefillResult:
        slot = seat[SEAT_FIELD]
        turn = self._saves.get((url, slot), 0)
        self._saves[url, slot] = (turn + 1) % FILES_PER_SLOT
        producer = hashlib.sha256(url.encode()).hexdigest()[:12]
        filename = f"narwhal-{producer}-{slot}-{turn}.bin"
        saved = await calls.post(
            url, f"/slots/{slot}?action=save", {"filename": filename}, timeout_s=timeout_s
        )
        descriptor = {"filename": filename, "tokens": saved.get("n_saved")}
        return PrefillResult(
            result.connector,
            result.producer_url,
            result.endpoint,
            result.producer_request_id,
            json.dumps(descriptor, allow_nan=False),
        )

    async def restore(
        self,
        calls: EngineCalls,
        url: str,
        seat: Mapping[str, Any],
        result: PrefillResult,
        *,
        timeout_s: float,
    ) -> None:
        filename = result.parameters().get("filename")
        if not isinstance(filename, str) or not filename:
            raise HandoffStepError(502, "the handoff names no saved slot file")
        await calls.post(
            url,
            f"/slots/{seat[SEAT_FIELD]}?action=restore",
            {"filename": filename},
            timeout_s=timeout_s,
        )
        return None
