from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from ...engines.connector import RendezvousConnector
from ...engines.dialect import EngineDialect
from ...types import Role

COMPLETION_PATHS = frozenset(("/v1/completions", "/v1/chat/completions"))


@dataclass(frozen=True)
class Pairing:
    role: Role
    peer: str
    # The launch record of whichever engine produces the KV.
    producer: Mapping[str, Any]


def origin(url: str | httpx.URL) -> str:
    parsed = httpx.URL(url)
    return f"{parsed.scheme}://{parsed.host}:{parsed.port}"


class _Joined(httpx.AsyncByteStream):
    def __init__(self, inner: httpx.AsyncByteStream, peer: asyncio.Task[None], wait_s: float):
        self.inner = inner
        self.peer = peer
        self.wait_s = wait_s

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for part in self.inner:
            yield part

    async def aclose(self) -> None:
        await self.inner.aclose()
        # The peer leg finishes with its KV transfer; a lost one ends at the decode wait.
        with contextlib.suppress(TimeoutError, httpx.HTTPError):
            async with asyncio.timeout(self.wait_s):
                await asyncio.shield(self.peer)
        if not self.peer.done():
            self.peer.cancel()
            await asyncio.gather(self.peer, return_exceptions=True)


class PairedTransport(httpx.AsyncBaseTransport):
    def __init__(
        self, inner: httpx.AsyncBaseTransport, kv: RendezvousConnector, dialect: EngineDialect
    ) -> None:
        self.inner = inner
        self.kv = kv
        self.dialect = dialect
        self.pairs: dict[str, Pairing] = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        pairing = self.pairs.get(origin(request.url))
        if pairing is None or request.method != "POST" or request.url.path not in COMPLETION_PATHS:
            return await self.inner.handle_async_request(request)
        body = json.loads(await request.aread())
        rendezvous = self.kv.rendezvous(pairing.producer)
        if pairing.role is Role.PREFILL:
            own = self.kv.prefill_body(body, rendezvous)
            peer = self.kv.decode_body(body, rendezvous)
        else:
            own = {**self.kv.decode_body(body, rendezvous), "stream": bool(body.get("stream"))}
            leg = {**body, "max_tokens": 1, "stream": False}
            for name in self.dialect.prefill_incompatible:
                leg.pop(name, None)
            peer = self.kv.prefill_body(leg, rendezvous)
        task = asyncio.create_task(
            self._drain(_rebuilt(request, pairing.peer + request.url.raw_path.decode(), peer))
        )
        try:
            response = await self.inner.handle_async_request(
                _rebuilt(request, str(request.url), own)
            )
        except BaseException:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        response.stream = _Joined(response.stream, task, self.kv.decode_wait_s)  # type: ignore[arg-type]
        return response

    async def _drain(self, request: httpx.Request) -> None:
        response = await self.inner.handle_async_request(request)
        try:
            async for _ in response.stream:  # type: ignore[union-attr]
                pass
        finally:
            await response.aclose()

    async def aclose(self) -> None:
        await self.inner.aclose()


def _rebuilt(request: httpx.Request, url: str, body: dict[str, Any]) -> httpx.Request:
    headers = {
        name: value
        for name, value in request.headers.items()
        if name.lower() not in ("host", "content-length")
    }
    return httpx.Request(
        request.method, url, headers=headers, json=body, extensions=request.extensions
    )
