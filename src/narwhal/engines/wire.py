from __future__ import annotations

import asyncio
import json
import select
import socket
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, cast
from urllib.parse import urlsplit

import httptools
import httpx

Dial = Callable[[str, int], Awaitable[socket.socket]]
GapTimeout = Callable[[], float | None]


async def dial_tcp(host: str, port: int) -> socket.socket:
    loop = asyncio.get_running_loop()
    failure: OSError | None = None
    for family, kind, proto, _, address in await loop.getaddrinfo(
        host, port, type=socket.SOCK_STREAM
    ):
        sock = socket.socket(family, kind, proto)
        sock.setblocking(False)
        try:
            await loop.sock_connect(sock, address)
        except OSError as exc:
            sock.close()
            failure = exc
            continue
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return sock
    raise failure or OSError(f"{host}:{port} resolved to no address")


class _Connection(asyncio.Protocol):
    transport: asyncio.Transport

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.closed = False
        self.idle_at = 0.0
        self._parser = httptools.HttpResponseParser(self)
        self._reset()

    def _reset(self) -> None:
        timer = getattr(self, "_timer", None)
        if timer is not None:
            timer.cancel()
        self.status = 0
        self.headers: list[tuple[bytes, bytes]] = []
        self.headers_done = False
        self.complete = False
        self.keep_alive = False
        self._chunks: deque[bytes] = deque()
        self._pending: list[bytes] = []
        self._error: BaseException | None = None
        self._waiter: asyncio.Future[None] | None = None
        self._last = self._wait_started = self.loop.time()
        self._timer: asyncio.TimerHandle | None = None
        self._gap: GapTimeout = lambda: None

    # asyncio.Protocol

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = cast(asyncio.Transport, transport)

    def data_received(self, data: bytes) -> None:
        self._last = self.loop.time()
        try:
            self._parser.feed_data(data)
        except httptools.HttpParserError as exc:
            self._fail(httpx.RemoteProtocolError(f"malformed engine response: {exc}"))
            return
        if self._pending:
            self._chunks.append(b"".join(self._pending))
            self._pending.clear()
        self._wake()

    def connection_lost(self, exc: Exception | None) -> None:
        self.closed = True
        if self.complete:
            return
        if exc is not None:
            self._fail(httpx.ReadError(f"engine connection lost: {exc}"))
        else:
            self._fail(
                httpx.RemoteProtocolError(
                    "peer closed connection without sending complete message body"
                )
            )

    # httptools callbacks

    def on_header(self, name: bytes, value: bytes) -> None:
        self.headers.append((name.lower(), value))

    def on_headers_complete(self) -> None:
        self.status = self._parser.get_status_code()
        self.keep_alive = self._parser.should_keep_alive()
        self.headers_done = True

    def on_body(self, body: bytes) -> None:
        self._pending.append(body)

    def on_message_complete(self) -> None:
        self.complete = True

    # exchange

    def send(self, request: bytes, gap: GapTimeout) -> None:
        self._reset()
        self._gap = gap
        self.transport.write(request)

    def _fail(self, exc: BaseException) -> None:
        if self._error is None:
            self._error = exc
        self._wake()

    def _wake(self) -> None:
        waiter = self._waiter
        if waiter is not None and not waiter.done():
            waiter.set_result(None)

    def _check_gap(self) -> None:
        self._timer = None
        gap = self._gap()
        if gap is None or self._waiter is None or self._waiter.done():
            return
        due = max(self._last, self._wait_started) + gap
        if self.loop.time() >= due:
            self._fail(httpx.ReadTimeout(f"engine sent nothing for {gap:g}s"))
        else:
            self._timer = self.loop.call_at(due, self._check_gap)

    async def wait(self) -> None:
        if self._error is not None:
            raise self._error
        self._waiter = self.loop.create_future()
        self._wait_started = self.loop.time()
        gap = self._gap()
        if gap is not None and self._timer is None:
            self._timer = self.loop.call_at(self._wait_started + gap, self._check_gap)
        try:
            await self._waiter
        finally:
            self._waiter = None
        if self._error is not None:
            raise self._error

    async def headers_ready(self) -> None:
        while not self.headers_done:
            await self.wait()

    async def chunks(self) -> AsyncIterator[bytes]:
        while True:
            while self._chunks:
                yield self._chunks.popleft()
            if self.complete:
                return
            await self.wait()

    def reusable(self, now: float, expiry_s: float) -> bool:
        if self.closed or now - self.idle_at >= expiry_s:
            return False
        sock = self.transport.get_extra_info("socket")
        if sock is None:
            return False
        # poll() accepts descriptors above FD_SETSIZE.
        poller = select.poll()
        poller.register(sock, select.POLLIN)
        return not poller.poll(0)

    def close(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if not self.closed:
            self.transport.close()
        self.closed = True


class WireResponse:
    def __init__(self, connection: _Connection) -> None:
        self._connection = connection
        self.status_code = connection.status
        self.headers = connection.headers
        self.body = b""

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        async for chunk in self._connection.chunks():
            yield chunk

    async def aread(self) -> bytes:
        self.body += b"".join([chunk async for chunk in self._connection.chunks()])
        return self.body

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self) -> Any:
        return json.loads(self.body)


class _Pool:
    def __init__(self, host: str, port: int, client: WireClient) -> None:
        self.host = host
        self.port = port
        self.client = client
        self.idle: deque[_Connection] = deque()
        self.open = 0
        self.waiters: deque[asyncio.Future[None]] = deque()

    async def acquire(
        self,
        pool_timeout_s: float,
        connect_timeout_s: float,
        on_slot: Callable[[], None] | None,
    ) -> _Connection:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + pool_timeout_s
        while True:
            while self.idle:
                connection = self.idle.pop()
                if connection.reusable(loop.time(), self.client.keepalive_expiry_s):
                    if on_slot is not None:
                        on_slot()
                    return connection
                connection.close()
                self.open -= 1
            if self.open < self.client.max_connections:
                self.open += 1
                if on_slot is not None:
                    on_slot()
                try:
                    return await self._connect(loop, connect_timeout_s)
                except BaseException:
                    self.open -= 1
                    self._release_slot()
                    raise
            waiter = loop.create_future()
            self.waiters.append(waiter)
            try:
                async with asyncio.timeout_at(deadline):
                    await waiter
            except TimeoutError as exc:
                raise httpx.PoolTimeout(
                    f"no connection to {self.host}:{self.port} within {pool_timeout_s:g}s"
                ) from exc
            finally:
                if waiter in self.waiters:
                    self.waiters.remove(waiter)

    async def _connect(
        self, loop: asyncio.AbstractEventLoop, connect_timeout_s: float
    ) -> _Connection:
        try:
            async with asyncio.timeout(connect_timeout_s):
                sock = await self.client.dial(self.host, self.port)
                _, connection = await loop.create_connection(lambda: _Connection(loop), sock=sock)
        except TimeoutError as exc:
            raise httpx.ConnectTimeout(
                f"connect to {self.host}:{self.port} exceeded {connect_timeout_s:g}s"
            ) from exc
        except OSError as exc:
            raise httpx.ConnectError(f"connect to {self.host}:{self.port} failed: {exc}") from exc
        return connection

    def release(self, connection: _Connection) -> None:
        reusable = (
            connection.complete
            and connection.keep_alive
            and not connection.closed
            and len(self.idle) < self.client.max_keepalive
        )
        if reusable:
            connection.idle_at = connection.loop.time()
            self.idle.append(connection)
        else:
            connection.close()
            self.open -= 1
        self._release_slot()

    def _release_slot(self) -> None:
        while self.waiters:
            waiter = self.waiters.popleft()
            if not waiter.done():
                waiter.set_result(None)
                return

    def close(self) -> None:
        for connection in self.idle:
            connection.close()
        self.idle.clear()


class WireClient:
    def __init__(
        self,
        *,
        max_connections: int,
        max_keepalive: int,
        connect_timeout_s: float,
        pool_timeout_s: float,
        keepalive_expiry_s: float,
        dial: Dial = dial_tcp,
    ) -> None:
        self.keepalive_expiry_s = keepalive_expiry_s
        self.max_connections = max_connections
        self.max_keepalive = max_keepalive
        self.connect_timeout_s = connect_timeout_s
        self.pool_timeout_s = pool_timeout_s
        self.dial = dial
        self._pools: dict[tuple[str, int], _Pool] = {}

    def _pool(self, host: str, port: int) -> _Pool:
        pool = self._pools.get((host, port))
        if pool is None:
            pool = self._pools[(host, port)] = _Pool(host, port, self)
        return pool

    @asynccontextmanager
    async def stream(
        self,
        url: str,
        body: dict[str, Any],
        headers: dict[str, str],
        *,
        gap: GapTimeout,
        budget_s: float | None = None,
        on_connection: Callable[[], None] | None = None,
    ) -> AsyncIterator[WireResponse]:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port or 80
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        pool_timeout_s = self.pool_timeout_s
        connect_timeout_s = self.connect_timeout_s
        if budget_s is not None:
            pool_timeout_s = min(pool_timeout_s, budget_s)
            connect_timeout_s = min(connect_timeout_s, budget_s)
        content = json.dumps(
            body, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
        head = [
            f"POST {target} HTTP/1.1",
            f"host: {parts.netloc}",
            "accept: */*",
            "accept-encoding: identity",
            "connection: keep-alive",
            "content-type: application/json",
            f"content-length: {len(content)}",
        ]
        head.extend(f"{name}: {value}" for name, value in headers.items())
        request = ("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + content
        pool = self._pool(host, port)
        connection = await pool.acquire(pool_timeout_s, connect_timeout_s, on_connection)
        try:
            connection.send(request, gap)
            await connection.headers_ready()
            yield WireResponse(connection)
        finally:
            pool.release(connection)

    async def post(
        self,
        url: str,
        body: dict[str, Any],
        headers: dict[str, str],
        *,
        timeout_s: float,
        on_connection: Callable[[], None] | None = None,
    ) -> WireResponse:
        async with self.stream(
            url,
            body,
            headers,
            gap=lambda: timeout_s,
            budget_s=timeout_s,
            on_connection=on_connection,
        ) as response:
            await response.aread()
        return response

    async def aclose(self) -> None:
        for pool in self._pools.values():
            pool.close()
        self._pools.clear()
