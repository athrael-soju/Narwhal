from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from ...config import EngineSpec
from ...engines.attestation import attested_launches
from ...engines.connector import KvHandoff
from ...engines.dialect import EngineDialect
from ...engines.handoff import HandoffLegs
from ...types import Role
from ..tasks import cancel_tasks

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
    def __init__(
        self, inner: httpx.AsyncByteStream, peer: asyncio.Task[None], wait_s: float | None
    ):
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
            await cancel_tasks([self.peer])


class PairedTransport(httpx.AsyncBaseTransport):
    """Send one leg of a concurrent handoff to one engine and its peer leg to the other."""

    def __init__(
        self, inner: httpx.AsyncBaseTransport, kv: KvHandoff, dialect: EngineDialect
    ) -> None:
        self.inner = inner
        self.kv = kv
        self.dialect = dialect
        self.pairs: dict[str, Pairing] = {}

    def pair(self, url: str, role: Role, peer: str, producer: Mapping[str, Any] | None) -> None:
        """Serve requests to `url` in `role`, with `peer` serving the other leg."""
        self.pairs[origin(url)] = Pairing(role, origin(peer), producer or {})

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        pairing = self.pairs.get(origin(request.url))
        if pairing is None or request.method != "POST" or request.url.path not in COMPLETION_PATHS:
            return await self.inner.handle_async_request(request)
        body = json.loads(await request.aread())
        handoff = HandoffLegs(self.kv, pairing.producer)
        url = str(request.url)
        endpoint = request.url.path
        if pairing.role is Role.PREFILL:
            own = self.kv.prefill_body(body, handoff.rendezvous)
            peer = handoff.decode_body(body, url=url, endpoint=endpoint)
        else:
            own = {
                **handoff.decode_body(body, url=url, endpoint=endpoint),
                "stream": bool(body.get("stream")),
            }
            peer = handoff.prefill_body(body, self.dialect)
        task = asyncio.create_task(
            self._drain(_rebuilt(request, pairing.peer + request.url.raw_path.decode(), peer))
        )
        try:
            response = await self.inner.handle_async_request(
                _rebuilt(request, str(request.url), own)
            )
        except BaseException:
            await cancel_tasks([task])
            raise
        wait_s = handoff.first_token_bound(None)
        response.stream = _Joined(response.stream, task, wait_s)  # type: ignore[arg-type]
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


async def paired_transport(
    inner: Callable[[], httpx.AsyncBaseTransport],
    kv: KvHandoff,
    dialect: EngineDialect,
    specs: list[EngineSpec],
    *,
    timeout_s: float,
) -> tuple[PairedTransport | None, dict[str, dict[str, Any]]]:
    if not kv.concurrent:
        return None, {}
    pairing = PairedTransport(inner(), kv, dialect)
    return pairing, await attested_launches(specs, timeout_s=timeout_s)


def _rebuilt(request: httpx.Request, url: str, body: dict[str, Any]) -> httpx.Request:
    headers = {
        name: value
        for name, value in request.headers.items()
        if name.lower() not in ("host", "content-length")
    }
    return httpx.Request(
        request.method, url, headers=headers, json=body, extensions=request.extensions
    )
