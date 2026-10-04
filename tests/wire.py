"""Serve HTTPX-style engine handlers to the wire client over socket pairs."""

import asyncio
import inspect
import socket
from collections.abc import Callable

import h11
import httpx

Handler = Callable[[httpx.Request], object]


class EngineWire:
    """Engine fake: each dial gets a socket pair whose far end runs `handler`.

    A handler that raises drops the connection without a response.
    """

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
        """Send `response`; return False when the client closed the connection first."""
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
    """Control-pool transport and data-leg dial for one engine handler."""
    return {"transport": httpx.MockTransport(handler), "dial": EngineWire(handler).dial}
