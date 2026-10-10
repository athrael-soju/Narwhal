import asyncio
import json
import os
import resource
import socket
import unittest

import httpx

from narwhal.engines.client import leg_failure_class
from narwhal.engines.wire import WireClient
from narwhal.types import LEG_CONNECTION, LEG_TIMEOUT


def chunked(*parts: bytes) -> bytes:
    return b"".join(b"%x\r\n%s\r\n" % (len(part), part) for part in parts) + b"0\r\n\r\n"


def head(status=200, **headers) -> bytes:
    lines = [f"HTTP/1.1 {status} OK"] + [
        f"{name.replace('_', '-')}: {value}" for name, value in headers.items()
    ]
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


class ScriptedEngine:
    def __init__(self, *scripts):
        self.scripts = list(scripts)
        self.connections = 0
        self.requests = []
        self.sockets = []
        self.tasks = set()

    async def dial(self, host, port):
        self.connections += 1
        near, far = socket.socketpair()
        near.setblocking(False)
        far.setblocking(False)
        self.sockets.append(far)
        task = asyncio.get_running_loop().create_task(self.serve(far))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return near

    async def serve(self, sock):
        reader, writer = await asyncio.open_connection(sock=sock)
        try:
            while self.scripts:
                request = await reader.readuntil(b"\r\n\r\n")
                length = next(
                    int(line.split(b":", 1)[1])
                    for line in request.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                )
                self.requests.append((request, await reader.readexactly(length)))
                for step in self.scripts.pop(0):
                    if step is None:
                        return
                    if isinstance(step, float):
                        await asyncio.sleep(step)
                        continue
                    writer.write(step)
                    await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            return
        finally:
            writer.close()

    async def aclose(self):
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


class WireClientTests(unittest.IsolatedAsyncioTestCase):
    def client(self, engine, **limits):
        settings = {
            "max_connections": 2,
            "max_keepalive": 1,
            "connect_timeout_s": 1.0,
            "pool_timeout_s": 1.0,
            "keepalive_expiry_s": 4.0,
        }
        client = WireClient(**(settings | limits), dial=engine.dial)
        self.addAsyncCleanup(client.aclose)
        self.addAsyncCleanup(engine.aclose)
        return client

    async def test_a_response_split_at_every_byte_reads_whole(self):
        body = b'{"count": 12}'
        for framing, raw in (
            ("length", head(content_length=len(body)) + body),
            ("chunked", head(transfer_encoding="chunked") + chunked(body[:5], body[5:])),
        ):
            with self.subTest(framing=framing):
                engine = ScriptedEngine([*(raw[i : i + 1] for i in range(len(raw)))])
                response = await self.client(engine).post(
                    "http://engine/tokenize", {"prompt": "x"}, {}, timeout_s=1.0
                )
                self.assertEqual((response.status_code, response.json()), (200, {"count": 12}))

    async def test_each_transport_read_is_one_body_chunk(self):
        frames = [b"data: a\n\n", b"data: b\n\n", b"data: c\n\n"]
        engine = ScriptedEngine(
            [
                head(transfer_encoding="chunked"),
                chunked(frames[0])[:-5],
                0.05,
                b"".join(b"%x\r\n%s\r\n" % (len(f), f) for f in frames[1:]),
                0.05,
                b"0\r\n\r\n",
            ]
        )
        async with self.client(engine).stream(
            "http://engine/v1/completions", {}, {}, gap=lambda: 1.0
        ) as response:
            chunks = [chunk async for chunk in response.aiter_bytes()]
        self.assertEqual(chunks, [frames[0], frames[1] + frames[2]])

    async def test_keep_alive_reuses_one_connection_and_sends_json_once(self):
        ok = head(content_length=2) + b"{}"
        engine = ScriptedEngine([ok], [ok], [ok])
        client = self.client(engine)
        for index in range(3):
            response = await client.post(
                "http://engine:8000/v1/completions",
                {"n": index, "text": "\u2028"},
                {"x-request-id": f"r{index}"},
                timeout_s=1.0,
            )
            self.assertEqual(response.status_code, 200)
        self.assertEqual(engine.connections, 1)
        request, body = engine.requests[-1]
        self.assertIn(b"host: engine:8000\r\n", request)
        self.assertIn(b"x-request-id: r2\r\n", request)
        self.assertEqual(json.loads(body), {"n": 2, "text": "\u2028"})

    async def test_an_idle_connection_past_the_keep_alive_expiry_is_replaced(self):
        ok = head(content_length=2) + b"{}"
        engine = ScriptedEngine([ok], [ok])
        client = self.client(engine)
        await client.post("http://engine/x", {}, {}, timeout_s=1.0)
        client.keepalive_expiry_s = 0.0
        response = await client.post("http://engine/x", {}, {}, timeout_s=1.0)
        self.assertEqual((response.status_code, engine.connections), (200, 2))

    async def test_reuse_stops_before_the_engine_closes_an_idle_connection(self):
        # vLLM closes a connection idle for 5 s; a request sent at that age fails unanswered.
        ok = head(content_length=2) + b"{}"
        engine = ScriptedEngine([ok], [ok])
        client = self.client(engine)
        await client.post("http://engine/x", {}, {}, timeout_s=1.0)
        (pool,) = client._pools.values()
        pool.idle[0].idle_at -= 4.5
        response = await client.post("http://engine/x", {}, {}, timeout_s=1.0)
        self.assertEqual((response.status_code, engine.connections), (200, 2))

    async def test_an_idle_connection_the_engine_closed_is_replaced(self):
        ok = head(content_length=2) + b"{}"
        engine = ScriptedEngine([ok], [ok])
        client = self.client(engine)
        await client.post("http://engine/x", {}, {}, timeout_s=1.0)
        # The engine's FIN arrives before the event loop reads it.
        engine.sockets[0].shutdown(socket.SHUT_WR)
        response = await client.post("http://engine/x", {}, {}, timeout_s=1.0)
        self.assertEqual((response.status_code, engine.connections), (200, 2))

    async def test_a_connection_above_the_select_descriptor_limit_is_reused(self):
        high = 1500
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if hard != resource.RLIM_INFINITY and hard <= high:
            self.skipTest(f"open-file limit {hard} is below descriptor {high}")
        if soft <= high:
            resource.setrlimit(resource.RLIMIT_NOFILE, (high + 1, hard))
            self.addCleanup(resource.setrlimit, resource.RLIMIT_NOFILE, (soft, hard))
        ok = head(content_length=2) + b"{}"
        engine = ScriptedEngine([ok], [ok])

        async def dial(host, port):
            sock = await engine.dial(host, port)
            moved = socket.socket(fileno=os.dup2(sock.fileno(), high))
            sock.close()
            moved.setblocking(False)
            return moved

        client = WireClient(
            max_connections=2,
            max_keepalive=1,
            connect_timeout_s=1.0,
            pool_timeout_s=1.0,
            keepalive_expiry_s=4.0,
            dial=dial,
        )
        self.addAsyncCleanup(client.aclose)
        self.addAsyncCleanup(engine.aclose)
        for _ in range(2):
            response = await client.post("http://engine/x", {}, {}, timeout_s=1.0)
            self.assertEqual(response.status_code, 200)
        self.assertEqual(engine.connections, 1)

    async def test_the_pool_limit_queues_then_times_out(self):
        engine = ScriptedEngine(
            [head(content_length=2), 0.3, b"{}"], [head(content_length=2) + b"{}"]
        )
        client = self.client(engine, max_connections=1, pool_timeout_s=0.05)
        held = asyncio.create_task(client.post("http://engine/a", {}, {}, timeout_s=1.0))
        await asyncio.sleep(0.02)
        with self.assertRaises(httpx.PoolTimeout) as caught:
            await client.post("http://engine/b", {}, {}, timeout_s=1.0)
        self.assertIsNone(leg_failure_class(caught.exception))
        self.assertEqual((await held).status_code, 200)
        response = await client.post("http://engine/b", {}, {}, timeout_s=1.0)
        self.assertEqual((response.status_code, engine.connections), (200, 1))

    async def test_connect_failure_and_connect_timeout(self):
        async def refused(host, port):
            raise ConnectionRefusedError("refused")

        async def silent(host, port):
            await asyncio.sleep(1.0)

        for dial, kind in ((refused, httpx.ConnectError), (silent, httpx.ConnectTimeout)):
            with self.subTest(kind=kind.__name__):
                client = WireClient(
                    max_connections=1,
                    max_keepalive=1,
                    connect_timeout_s=0.05,
                    pool_timeout_s=1.0,
                    keepalive_expiry_s=4.0,
                    dial=dial,
                )
                self.addAsyncCleanup(client.aclose)
                with self.assertRaises(kind) as caught:
                    await client.post("http://engine/x", {}, {}, timeout_s=1.0)
                expected = LEG_CONNECTION
                self.assertEqual(leg_failure_class(caught.exception), expected)

    async def test_header_and_gap_deadlines(self):
        for name, script in (
            ("header", [0.3, head(content_length=2) + b"{}"]),
            ("gap", [head(transfer_encoding="chunked"), chunked(b"a")[:-5], 0.3, b"0\r\n\r\n"]),
        ):
            with self.subTest(deadline=name):
                engine = ScriptedEngine(script)
                with self.assertRaises(httpx.ReadTimeout) as caught:
                    await self.client(engine).post("http://engine/x", {}, {}, timeout_s=0.1)
                self.assertEqual(leg_failure_class(caught.exception), LEG_TIMEOUT)

    async def test_the_gap_deadline_moves_with_each_read(self):
        steps = [head(transfer_encoding="chunked")]
        for _ in range(6):
            steps += [0.04, b"1\r\na\r\n"]
        engine = ScriptedEngine([*steps, b"0\r\n\r\n"])
        response = await self.client(engine).post("http://engine/x", {}, {}, timeout_s=0.1)
        self.assertEqual(response.body, b"aaaaaa")

    async def test_an_engine_closing_mid_body_is_a_protocol_error(self):
        engine = ScriptedEngine([head(content_length=10) + b"abc", None])
        with self.assertRaisesRegex(httpx.RemoteProtocolError, "peer closed connection"):
            await self.client(engine).post("http://engine/x", {}, {}, timeout_s=1.0)

    async def test_error_statuses_keep_their_bodies_and_the_connection(self):
        busy = head(status=503, content_length=11) + b"engine busy"
        engine = ScriptedEngine([busy], [head(content_length=2) + b"{}"])
        client = self.client(engine)
        response = await client.post("http://engine/x", {}, {}, timeout_s=1.0)
        self.assertEqual((response.status_code, response.text), (503, "engine busy"))
        response = await client.post("http://engine/x", {}, {}, timeout_s=1.0)
        self.assertEqual((response.status_code, engine.connections), (200, 1))

    async def test_an_abandoned_stream_closes_its_connection(self):
        engine = ScriptedEngine(
            [head(transfer_encoding="chunked"), chunked(b"a")[:-5], 1.0],
            [head(content_length=2) + b"{}"],
        )
        client = self.client(engine)
        async with client.stream("http://engine/x", {}, {}, gap=lambda: 1.0) as response:
            await anext(aiter(response.aiter_bytes()))
        response = await client.post("http://engine/x", {}, {}, timeout_s=1.0)
        self.assertEqual((response.status_code, engine.connections), (200, 2))


if __name__ == "__main__":
    unittest.main()
