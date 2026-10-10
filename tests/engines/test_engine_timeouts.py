import asyncio
import contextlib
import json
import unittest
from unittest.mock import patch

import httpx

from narwhal.engines.client import (
    FIRST_OUTPUT_DETAIL,
    STREAM_SILENCE_DETAIL,
    EngineClient,
    EngineError,
    ProbeLeg,
    leg_failure_class,
)
from narwhal.types import LEG_TIMEOUT
from tests.wire import vllm_engine

TOKEN = b'data: {"choices":[{"text":"x","token_ids":[1]}]}\n\n'
DONE = b"data: [DONE]\n\n"


class EngineTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.header_delay = 0
        self.timeline = []
        self.handlers = set()
        self.client = EngineClient(**vllm_engine(), read_timeout_s=0.04)

        async def serve(reader, writer):
            task = asyncio.current_task()
            self.handlers.add(task)
            try:
                headers = await reader.readuntil(b"\r\n\r\n")
                size = next(
                    int(line.split(b":", 1)[1])
                    for line in headers.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                )
                await reader.readexactly(size)
                await asyncio.sleep(self.header_delay)
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                    b"Connection: close\r\n\r\n"
                )
                await writer.drain()
                for delay, payload in self.timeline:
                    await asyncio.sleep(delay)
                    writer.write(payload)
                    await writer.drain()
            except (ConnectionError, asyncio.IncompleteReadError):
                pass
            finally:
                writer.close()
                with contextlib.suppress(ConnectionError):
                    await writer.wait_closed()
                self.handlers.discard(task)

        self.server = await asyncio.start_server(serve, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"

    async def asyncTearDown(self):
        await self.client.aclose()
        self.server.close()
        await self.server.wait_closed()
        pending = list(self.handlers)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def consume(self, budget=0.4):
        return [
            event.line
            async for batch in self.client.decode(
                self.url,
                "/v1/completions",
                {"model": "stub", "prompt": "x"},
                {},
                None,
                first_token_timeout_s=budget,
            )
            for event in batch
        ]

    async def test_first_token_budget_covers_response_headers(self):
        self.header_delay = 0.12
        self.timeline = [(0, TOKEN + DONE)]
        self.assertEqual((await self.consume())[-1], "data: [DONE]")

    async def test_first_token_can_arrive_after_chunk_timeout(self):
        self.timeline = [(0.12, TOKEN + DONE)]
        self.assertEqual((await self.consume())[-1], "data: [DONE]")

    async def test_headers_and_first_token_share_one_deadline(self):
        await self.client.aclose()
        self.client = EngineClient(**vllm_engine(), read_timeout_s=0.3)
        self.header_delay = 0.08
        self.timeline = [(0.08, TOKEN + DONE)]
        with self.assertRaisesRegex(EngineError, FIRST_OUTPUT_DETAIL):
            await self.consume(budget=0.12)

    async def test_metadata_does_not_reset_first_token_deadline(self):
        self.timeline = [(0.02, b": keepalive\n\n")] * 12 + [(0, TOKEN + DONE)]
        with self.assertRaisesRegex(EngineError, FIRST_OUTPUT_DETAIL):
            await self.consume(budget=0.12)

    async def test_chunk_timeout_applies_after_first_token(self):
        self.timeline = [(0, TOKEN), (0.12, DONE)]
        with self.assertRaisesRegex(EngineError, STREAM_SILENCE_DETAIL):
            await self.consume()

    async def test_zero_read_timeout_disables_chunk_gap_bound(self):
        await self.client.aclose()
        self.client = EngineClient(**vllm_engine(), read_timeout_s=0)
        self.timeline = [(0, TOKEN), (0.12, DONE)]
        self.assertEqual((await self.consume())[-1], "data: [DONE]")

    async def test_partial_lines_reset_transport_gap_timeout(self):
        self.timeline = [(0, TOKEN)] + [(0.015, bytes([byte])) for byte in DONE]
        self.assertEqual((await self.consume())[-1], "data: [DONE]")

    async def test_header_deadline_releases_connection_slot(self):
        await self.client.aclose()
        self.client = EngineClient(**vllm_engine(), read_timeout_s=0.3, max_connections=1)
        self.header_delay = 0.12
        self.timeline = [(0, TOKEN + DONE)]
        with self.assertRaisesRegex(EngineError, FIRST_OUTPUT_DETAIL):
            await self.consume(budget=0.04)
        self.header_delay = 0
        self.assertEqual((await self.consume())[-1], "data: [DONE]")

    async def test_inference_probe_uses_its_first_token_budget(self):
        self.client.model = "stub"
        self.header_delay = 0.08
        self.timeline = [(0.08, TOKEN + DONE)]
        with patch.object(self.client, "_probe_prefill", return_value=ProbeLeg()):
            result = await self.client.probe_inference(self.url, deadline_s=0.4)
        self.assertIsNone(result.decode.failed)


class PrefillPoolDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.release = asyncio.Event()
        self.requests = []
        self.handlers = set()

        async def serve(reader, writer):
            task = asyncio.current_task()
            self.handlers.add(task)
            try:
                while True:
                    headers = await reader.readuntil(b"\r\n\r\n")
                    size = next(
                        int(line.split(b":", 1)[1])
                        for line in headers.split(b"\r\n")
                        if line.lower().startswith(b"content-length:")
                    )
                    await reader.readexactly(size)
                    path = headers.split(b" ")[1].decode()
                    self.requests.append(path)
                    if path == "/slow":
                        await self.release.wait()
                    body = (
                        b"x"
                        if path == "/occupied"
                        else json.dumps(
                            {
                                "kv_transfer_params": {
                                    "remote_engine_id": "e0",
                                    "remote_block_ids": [0],
                                }
                            }
                        ).encode()
                    )
                    writer.write(
                        b"HTTP/1.1 200 OK\r\nContent-Length: "
                        + str(len(body)).encode()
                        + b"\r\n\r\n"
                    )
                    await writer.drain()
                    if path == "/occupied":
                        await self.release.wait()
                    writer.write(body)
                    await writer.drain()
            except (ConnectionError, asyncio.IncompleteReadError):
                pass
            finally:
                writer.close()
                with contextlib.suppress(ConnectionError):
                    await writer.wait_closed()
                self.handlers.discard(task)

        self.server = await asyncio.start_server(serve, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"

    async def asyncTearDown(self):
        self.release.set()
        self.server.close()
        await self.server.wait_closed()
        pending = list(self.handlers)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def test_elapsed_deadline_during_pool_wait_is_not_breaker_evidence(self):
        self.release.clear()
        self.requests.clear()
        client = EngineClient(
            **vllm_engine(), max_connections=1, prefill_timeout_s=0.04, pool_timeout_s=1
        )
        try:
            async with client._wire.stream(
                self.url + "/occupied", {}, {}, gap=lambda: None
            ) as held:
                with self.assertRaises(httpx.PoolTimeout) as caught:
                    await client.prefill(self.url, "/v1/completions", {"prompt": "x"}, {})
                self.assertIsNone(leg_failure_class(caught.exception))
                self.assertEqual(self.requests, ["/occupied"])
                self.release.set()
                await held.aread()
            result = await client.prefill(self.url, "/v1/completions", {"prompt": "x"}, {})
            self.assertEqual(result.parameters()["remote_engine_id"], "e0")
        finally:
            await client.aclose()

    async def test_a_held_connection_to_one_engine_leaves_other_engines_free(self):
        self.release.clear()
        self.requests.clear()
        client = EngineClient(
            **vllm_engine(), max_connections=1, prefill_timeout_s=0.5, pool_timeout_s=0.2
        )
        other = self.url.replace("127.0.0.1", "localhost")
        try:
            async with client._wire.stream(
                self.url + "/occupied", {}, {}, gap=lambda: None
            ) as held:
                result = await client.prefill(other, "/v1/completions", {"prompt": "x"}, {})
                self.assertEqual(result.parameters()["remote_engine_id"], "e0")
                self.release.set()
                await held.aread()
            port = int(self.url.rsplit(":", 1)[1])
            wire = client._wire
            self.assertIsNot(wire._pool("127.0.0.1", port), wire._pool("localhost", port))
            self.assertIs(wire._pool("localhost", port), wire._pool("localhost", port))
        finally:
            await client.aclose()

    async def test_elapsed_deadline_after_dispatch_remains_engine_timeout(self):
        for reused_connection in (False, True):
            with self.subTest(reused_connection=reused_connection):
                self.requests.clear()
                client = EngineClient(**vllm_engine(), prefill_timeout_s=0.5, pool_timeout_s=1)
                try:
                    if reused_connection:
                        await client.prefill(self.url, "/v1/completions", {"prompt": "x"}, {})
                    with self.assertRaises(httpx.ReadTimeout) as caught:
                        await client.prefill(self.url, "/slow", {"prompt": "x"}, {})
                    self.assertEqual(leg_failure_class(caught.exception), LEG_TIMEOUT)
                    self.assertEqual(self.requests[-1], "/slow")
                finally:
                    await client.aclose()


class HealthProbeLatenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_silent_engine_fails_a_probe_that_queued_for_the_control_pool(self):

        async def silent(reader, writer):
            await reader.read()
            writer.close()

        server = await asyncio.start_server(silent, "127.0.0.1", 0)
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)
        url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        client = EngineClient(
            **vllm_engine(), control_connections=1, health_timeout_s=0.3, pool_timeout_s=5.0
        )
        self.addAsyncCleanup(client.aclose)

        async def probe(delay):
            await asyncio.sleep(delay)
            return await client.healthy(url)

        self.assertEqual(await asyncio.gather(probe(0.0), probe(0.1)), [False, False])
