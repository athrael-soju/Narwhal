from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from collections.abc import Mapping
from typing import Any, ClassVar

from ...engines.connector import (
    EngineCalls,
    HandoffPhase,
    HandoffStepError,
    KvConnector,
    PrefillResult,
)
from .identity import PROPS_PATH, slot_count

SEAT_FIELD = "id_slot"
PROPS_TIMEOUT_S = 2.0


class SlotFileConnector(KvConnector):
    """Hand KV state over as a slot file: the producer saves its slot and the consumer restores it.

    Every engine of a fleet starts with the same --slot-save-path directory. A producer keeps
    as many files there as it has had handoffs in flight at once.
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
        # File numbers each producer's handoffs hold from `export` until `discard`, so a
        # consumer never restores a file its producer is rewriting.
        self._held: dict[str, set[int]] = {}
        # Routers that share a slot directory write separate files.
        self._router = secrets.token_hex(4)

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
            slots = slot_count(await calls.get(url, PROPS_PATH, timeout_s=PROPS_TIMEOUT_S))
            if slots is None:
                raise HandoffStepError(502, f"{PROPS_PATH} reports no total_slots")
            free = self._free.setdefault(url, list(range(slots - 1, -1, -1)))
        if free:
            return {SEAT_FIELD: free.pop()}
        # The caller's deadline cancels the wait.
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
        waiting = self._waiting.get(url, [])
        while waiting:
            # A cancelled wait stays listed until its task next runs.
            turn = waiting.pop(0)
            if not turn.done():
                turn.set_result(slot)
                return
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
        held = self._held.setdefault(url, set())
        number = next(n for n in range(len(held) + 1) if n not in held)
        held.add(number)
        producer = hashlib.sha256(url.encode()).hexdigest()[:12]
        filename = f"narwhal-{self._router}-{producer}-{number}.bin"
        try:
            saved = await calls.post(
                url,
                f"/slots/{seat[SEAT_FIELD]}?action=save",
                {"filename": filename},
                timeout_s=timeout_s,
            )
        except BaseException:
            held.discard(number)
            raise
        descriptor = {"filename": filename, "file": number, "tokens": saved.get("n_saved")}
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

    def discard(self, result: PrefillResult) -> None:
        # The file stays on disk and the producer's next handoff overwrites it.
        number = result.parameters().get("file")
        if type(number) is int:
            self._held.get(result.producer_url, set()).discard(number)
        return None
