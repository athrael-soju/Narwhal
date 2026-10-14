from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from typing import Any

from .connector import KvHandoff, PrefillResult
from .dialect import EngineDialect

PrefillLeg = Callable[[dict[str, Any] | None], Awaitable[PrefillResult | None]]


class HandoffLegs:
    """One request's prefill and decode legs, run in the order the connector declares."""

    def __init__(self, kv: KvHandoff, producer: Mapping[str, Any] | None = None) -> None:
        self.kv = kv
        self.connector = kv.name
        self.rendezvous = kv.rendezvous(producer or {})
        self.result: PrefillResult | None = None
        self.prefill_seconds: float | None = None
        # A concurrent prefill leg runs while the decode leg streams.
        self.task: asyncio.Task[PrefillResult | None] | None = None

    async def start(self, leg: PrefillLeg) -> HandoffLegs:
        started = time.monotonic()

        async def run() -> PrefillResult | None:
            result = await leg(self.rendezvous)
            self.result = result
            self.prefill_seconds = time.monotonic() - started
            return result

        if self.kv.concurrent:
            self.task = asyncio.create_task(run())
        else:
            await run()
        return self

    def prefill_body(self, body: dict[str, Any], dialect: EngineDialect) -> dict[str, Any]:
        return prefill_leg(body, self.kv, dialect, self.rendezvous)

    def decode_body(self, body: dict[str, Any], *, url: str, endpoint: str) -> dict[str, Any]:
        return self.kv.decode_body(
            body, result=self.result, rendezvous=self.rendezvous, url=url, endpoint=endpoint
        )

    def first_token_bound(self, limit: float | None) -> float | None:
        return self.kv.first_token_bound(limit)

    def failure(self) -> BaseException | None:
        task = self.task
        if task is None or not task.done() or task.cancelled():
            return None
        return task.exception()

    async def settle(self, *, cancel: bool) -> BaseException | None:
        task = self.task
        if task is None:
            return None
        if cancel and not task.done():
            task.cancel()
        with suppress(Exception, asyncio.CancelledError):
            await task
        return None if task.cancelled() else task.exception()

    @property
    def descriptor_json(self) -> str:
        if self.result is not None:
            return self.result.descriptor_json
        return json.dumps(self.rendezvous or {}, sort_keys=True)

    @property
    def parameters(self) -> dict[str, Any]:
        return self.result.parameters() if self.result is not None else dict(self.rendezvous or {})

    def diagnostics(self) -> dict[str, Any]:
        parameters = self.parameters
        return {name: parameters.get(name) for name in self.kv.diagnostics_fields}


def prefill_leg(
    body: dict[str, Any],
    kv: KvHandoff,
    dialect: EngineDialect,
    rendezvous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    leg = kv.prefill_body({**body, "max_tokens": 1, "stream": False}, rendezvous)
    for name in dialect.prefill_incompatible:
        leg.pop(name, None)
    return leg
