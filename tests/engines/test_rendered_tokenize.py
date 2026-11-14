import asyncio
import json
import unittest
from typing import Any

import httpx

from narwhal.backends import load
from narwhal.engines.client import EngineClient, EngineError
from tests.wire import engine_transports

MESSAGES = [{"role": "user", "content": "hello"}]
RENDERED = "<|user|>hello<|assistant|>"


class Rendering(type(load("vllm").dialect)):  # type: ignore[misc]
    """A dialect whose tokenize route takes text, with a route that renders chat messages."""

    render_path = "/render"

    def tokenize_request(self, model: str | None, body: dict[str, Any]) -> dict[str, Any]:
        return {"content": body.get("prompt", ""), "add_special": True}

    def rendered_tokenize_request(self, model: str | None, prompt: str) -> dict[str, Any]:
        return {"content": prompt, "add_special": False}


class RenderedTokenizeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls: list[tuple[str, dict]] = []
        self.responses: dict[str, httpx.Response] = {
            "/render": httpx.Response(200, json={"prompt": RENDERED}),
            "/tokenize": httpx.Response(200, json={"count": 3, "tokens": [1, 7, 9]}),
        }
        self.delay = 0.0
        self.client = EngineClient(
            kv=load("vllm").connector("nixl"),
            dialect=Rendering(),
            model="stub",
            **engine_transports(self.handle),
        )
        self.addAsyncCleanup(self.client.aclose)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.url.path, json.loads(request.content)))
        await asyncio.sleep(self.delay)
        return self.responses[request.url.path]

    async def tokenize(self, body: dict, *, strict: bool = False, timeout_s: float = 1):
        return await self.client.tokenize("http://engine", body, timeout_s, strict=strict)

    async def test_chat_messages_are_rendered_and_the_render_is_counted(self):
        body = {"model": "stub", "messages": MESSAGES, "tools": [{"type": "function"}]}
        result = await self.tokenize(body)
        self.assertEqual((result.count, result.token_ids), (3, (1, 7, 9)))
        self.assertEqual(
            self.calls,
            [
                ("/render", {"model": "stub", "messages": MESSAGES, "tools": body["tools"]}),
                ("/tokenize", {"content": RENDERED, "add_special": False}),
            ],
        )

    async def test_a_completion_prompt_goes_to_the_tokenize_route_alone(self):
        await self.tokenize({"model": "stub", "prompt": "hello"})
        self.assertEqual(self.calls, [("/tokenize", {"content": "hello", "add_special": True})])

    async def test_a_failed_render_gives_no_count_and_a_strict_call_names_it(self):
        for response, status, detail in (
            (httpx.Response(503, text="loading"), 503, "loading"),
            (httpx.Response(200, text="{"), 502, "invalid JSON response"),
            (httpx.Response(200, json=[]), 502, "invalid JSON response"),
            (httpx.Response(200, json={"prompt": 3}), 502, "response has no rendered prompt"),
        ):
            with self.subTest(detail=detail):
                self.responses["/render"] = response
                self.calls.clear()
                self.assertIsNone(await self.tokenize({"messages": MESSAGES}))
                with self.assertRaises(EngineError) as raised:
                    await self.tokenize({"messages": MESSAGES}, strict=True)
                self.assertEqual(
                    (raised.exception.leg, raised.exception.status, raised.exception.detail),
                    ("tokenize", status, detail),
                )
                self.assertEqual([path for path, _ in self.calls], ["/render", "/render"])

    async def test_one_deadline_covers_the_render_and_the_count(self):
        self.delay = 0.06
        self.assertIsNone(await self.tokenize({"messages": MESSAGES}, timeout_s=0.1))
        with self.assertRaisesRegex(EngineError, "exact count exceeded 0.1s"):
            await self.tokenize({"messages": MESSAGES}, strict=True, timeout_s=0.1)
        self.assertEqual((await self.tokenize({"messages": MESSAGES}, timeout_s=1)).count, 3)
