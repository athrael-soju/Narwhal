"""Exercise decode deadlines through real HTTP transport reads."""

import asyncio
import contextlib
import unittest
from unittest.mock import patch

from narwhal.engines.client import (
    FIRST_OUTPUT_DETAIL,
    STREAM_SILENCE_DETAIL,
    EngineClient,
    EngineError,
    ProbeLeg,
)

TOKEN = b'data: {"choices":[{"text":"x","token_ids":[1]}]}\n\n'
DONE = b"data: [DONE]\n\n"


class EngineTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.header_delay = 0
        self.timeline = []
        self.handlers = set()
        self.client = EngineClient(read_timeout_s=0.04)

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
            line
            async for line in self.client.decode(
                self.url,
                "/v1/completions",
                {"model": "stub", "prompt": "x"},
                {},
                None,
                first_token_timeout_s=budget,
            )
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
        self.client = EngineClient(read_timeout_s=0.3)
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
        self.client = EngineClient(read_timeout_s=0)
        self.timeline = [(0, TOKEN), (0.12, DONE)]
        self.assertEqual((await self.consume())[-1], "data: [DONE]")

    async def test_partial_lines_reset_transport_gap_timeout(self):
        self.timeline = [(0, TOKEN)] + [(0.015, bytes([byte])) for byte in DONE]
        self.assertEqual((await self.consume())[-1], "data: [DONE]")

    async def test_header_deadline_releases_connection_slot(self):
        await self.client.aclose()
        self.client = EngineClient(read_timeout_s=0.3, max_connections=1)
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
