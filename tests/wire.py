import asyncio
import inspect
import itertools
import json
import socket
import tempfile
import unittest
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

import h11
import httpx

from narwhal.backends import load
from narwhal.config import SLO, EngineSpec, FleetConfig
from narwhal.engines.client import EngineClient
from narwhal.engines.connector import RendezvousConnector
from narwhal.profiling.model import Profile
from narwhal.serving.app import create_app
from narwhal.serving.router.routing import NarwhalRouter
from narwhal.types import Role

Handler = Callable[[httpx.Request], object]


class EngineWire:
    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.dials: list[tuple[str, int]] = []
        self._tasks: set[asyncio.Task] = set()

    async def dial(self, host: str, port: int) -> socket.socket:
        self.dials.append((host, port))
        near, far = socket.socketpair()
        near.setblocking(False)
        far.setblocking(False)
        task = asyncio.get_running_loop().create_task(self._serve(far, host, port))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return near

    async def _serve(self, sock: socket.socket, host: str, port: int) -> None:
        reader, writer = await asyncio.open_connection(sock=sock)
        connection = h11.Connection(h11.SERVER)
        try:
            while True:
                request = await self._request(connection, reader, host, port)
                if request is None:
                    return
                try:
                    response = self.handler(request)
                    if inspect.isawaitable(response):
                        response = await response
                except Exception:
                    return
                if not await self._respond(connection, reader, writer, response):
                    return
                if connection.our_state is h11.MUST_CLOSE:
                    return
                connection.start_next_cycle()
        except (ConnectionError, h11.RemoteProtocolError):
            return
        finally:
            writer.close()

    @staticmethod
    async def _request(
        connection: h11.Connection, reader: asyncio.StreamReader, host: str, port: int
    ) -> httpx.Request | None:
        head = None
        body = b""
        while True:
            event = connection.next_event()
            if event is h11.NEED_DATA:
                data = await reader.read(65536)
                connection.receive_data(data)
                if not data and head is None:
                    return None
                continue
            if isinstance(event, h11.ConnectionClosed):
                return None
            if isinstance(event, h11.Request):
                head = event
            elif isinstance(event, h11.Data):
                body += event.data
            elif isinstance(event, h11.EndOfMessage):
                assert head is not None
                return httpx.Request(
                    head.method.decode(),
                    f"http://{host}:{port}{head.target.decode()}",
                    headers=[(name, value) for name, value in head.headers],
                    content=body,
                )

    @staticmethod
    async def _respond(
        connection: h11.Connection,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        response: httpx.Response,
    ) -> bool:
        length = response.headers.get("content-length")
        headers = [
            (name, value)
            for name, value in response.headers.raw
            if name.lower() not in (b"content-length", b"transfer-encoding")
        ]
        headers.append(
            (b"content-length", length.encode())
            if length is not None
            else (b"transfer-encoding", b"chunked")
        )

        async def send() -> None:
            writer.write(
                connection.send(h11.Response(status_code=response.status_code, headers=headers))
            )
            await writer.drain()
            async for chunk in response.stream:
                if chunk:
                    writer.write(connection.send(h11.Data(data=chunk)))
                    await writer.drain()
            writer.write(connection.send(h11.EndOfMessage()))
            await writer.drain()

        sending = asyncio.ensure_future(send())
        closing = asyncio.ensure_future(reader.read(1))
        try:
            await asyncio.wait({sending, closing}, return_when=asyncio.FIRST_COMPLETED)
            if not sending.done():
                sending.cancel()
                await asyncio.gather(sending, return_exceptions=True)
                return False
            sending.result()
            return True
        finally:
            closing.cancel()
            await asyncio.gather(closing, return_exceptions=True)
            await response.aclose()

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)


def engine_transports(handler: Handler) -> dict:
    return {"transport": httpx.MockTransport(handler), "dial": EngineWire(handler).dial}


def engine_clients(module: ModuleType, handler: Handler) -> AbstractContextManager:
    return patch.object(
        module,
        "EngineClient",
        side_effect=lambda **kwargs: EngineClient(**kwargs, **engine_transports(handler)),
    )


def routed(handler: Handler) -> type[httpx.AsyncClient]:
    base = httpx.AsyncClient

    class Routed(base):
        def __init__(self, *args, transport=None, **kwargs):
            super().__init__(*args, transport=transport or httpx.MockTransport(handler), **kwargs)

    return Routed


def serving_app(
    case: unittest.IsolatedAsyncioTestCase,
    handler: Handler,
    *,
    urls: tuple[str, str] = ("http://prefill", "http://decode"),
    tokenize: bool = False,
) -> tuple[NarwhalRouter, httpx.AsyncClient]:
    """Serve a profiled stub prefill and decode pair over handler; register its cleanups."""
    directory = tempfile.TemporaryDirectory()
    case.addCleanup(directory.cleanup)
    cfg = FleetConfig(
        model="stub",
        slo=SLO(ttft_s=10.0, tpot_s=0.5),
        engines=[
            EngineSpec("p", urls[0], Role.PREFILL),
            EngineSpec("d", urls[1], Role.DECODE),
        ],
        profiles_path=Path(directory.name) / "profiles.json",
        tokenize=tokenize,
        admission="open",
    )

    def wired_router(*args, **kwargs):
        return NarwhalRouter(*args, **engine_transports(handler), **kwargs)

    with patch("narwhal.serving.app.NarwhalRouter", side_effect=wired_router):
        app = create_app(cfg)
    router = app.state.router
    case.addAsyncCleanup(router.engines.aclose)
    for iid in ("p", "d"):
        router.profiles.put(
            Profile(
                iid,
                2e-8,
                6e-5,
                0.005,
                3e-6,
                0.012,
                decode_min_requests=1,
                decode_max_requests=4,
                decode_min_kv_tokens=1,
                decode_max_kv_tokens=1024,
                decode_fit_mape=0.0,
                decode_cv_mape=0.0,
            )
        )
    router.journal.open()
    case.addCleanup(router.journal.close)
    # No monitoring lifespan or network fleet is needed for HTTP serving.
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router")
    case.addAsyncCleanup(client.aclose)
    return router, client


def vllm_engine() -> dict:
    backend = load("vllm")
    return {"kv": backend.connector("nixl"), "dialect": backend.dialect}


class FakeRendezvous(RendezvousConnector):
    name = "fake"
    decode_wait_s = 0.3

    def __init__(self) -> None:
        self.rooms = itertools.count()

    def handoff_bound(self, lease_s: int) -> float:
        return float(lease_s)

    def rendezvous(self, producer: Mapping[str, Any]) -> dict[str, Any]:
        return {"room": next(self.rooms)}

    def prefill_body(self, body: dict[str, Any], rendezvous: dict[str, Any]) -> dict[str, Any]:
        return {**body, "leg": "prefill", **rendezvous}

    def decode_body(self, body: dict[str, Any], rendezvous: dict[str, Any]) -> dict[str, Any]:
        return {**body, "leg": "decode", **rendezvous}


class RendezvousLegs:
    def __init__(self, frames: list[dict[str, Any]]) -> None:
        self.frames = frames
        self.calls: list[tuple[httpx.Request, dict[str, Any]]] = []
        self.prefill_status = 200
        self.prefill_seen = asyncio.Event()
        self.decode_seen = asyncio.Event()

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.calls.append((request, body))
        if body.get("leg") == "prefill":
            self.prefill_seen.set()
            # Each leg waits for the other, so only concurrent legs complete.
            await asyncio.wait_for(self.decode_seen.wait(), 5)
            return httpx.Response(self.prefill_status, json={})
        self.decode_seen.set()
        if self.prefill_status != 200:
            await asyncio.Event().wait()
        await asyncio.wait_for(self.prefill_seen.wait(), 5)
        wire = "".join("data: " + json.dumps(frame) + "\n\n" for frame in self.frames)
        return httpx.Response(200, text=wire + "data: [DONE]\n\n")
