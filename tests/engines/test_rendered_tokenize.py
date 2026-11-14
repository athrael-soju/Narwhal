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
CHAT = {"model": "stub", "messages": MESSAGES}
Base = type(load("vllm").dialect)


class Rendering(Base):  # type: ignore[misc, valid-type]
    """A dialect with a render route and the base tokenize requests."""

    render_path = "/render"
    render_passthrough = ("tools",)


class TextOnly(Rendering):
    """A dialect whose tokenize route takes text and answers with token IDs alone."""

    def tokenize_request(self, model: str | None, body: dict[str, Any]) -> dict[str, Any]:
        return {"content": body.get("prompt", ""), "add_special": True}

    def rendered_tokenize_request(
        self, model: str | None, prompt: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        return {"content": prompt, "add_special": body.get("add_special_tokens", True)}

    def tokenize_response(self, payload: dict[str, Any]) -> int | None:
        tokens = payload.get("tokens") if isinstance(payload, dict) else None
        return len(tokens) if isinstance(tokens, list) else None


class RenderedTokenizeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls: list[tuple[str, dict]] = []
        self.responses: dict[str, httpx.Response | Exception] = {
            "/render": httpx.Response(200, json={"prompt": RENDERED}),
            "/tokenize": httpx.Response(200, json={"count": 3, "tokens": [1, 7, 9]}),
        }
        self.delay = 0.0
        self.client = self.engine(Rendering())

    def engine(self, dialect) -> EngineClient:
        client = EngineClient(
            kv=load("vllm").connector("nixl"),
            dialect=dialect,
            model="stub",
            **engine_transports(self.handle),
        )
        self.addAsyncCleanup(client.aclose)
        return client

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.url.path, json.loads(request.content)))
        await asyncio.sleep(self.delay)
        response = self.responses[request.url.path]
        if isinstance(response, Exception):
            raise response
        return response

    def paths(self) -> list[str]:
        return [path for path, _ in self.calls]

    async def tokenize(self, body: dict, *, strict: bool = False, timeout_s: float = 1):
        return await self.client.tokenize("http://engine", body, timeout_s, strict=strict)

    async def failure(self, body: dict) -> tuple[int, str]:
        self.assertIsNone(await self.tokenize(body))
        with self.assertRaises(EngineError) as raised:
            await self.tokenize(body, strict=True)
        self.assertEqual(raised.exception.leg, "tokenize")
        return raised.exception.status, raised.exception.detail

    async def test_chat_messages_are_rendered_and_the_render_is_counted(self):
        body = {**CHAT, "tools": [{"type": "function"}], "add_special_tokens": True}
        result = await self.tokenize(body)
        self.assertEqual((result.count, result.token_ids), (3, (1, 7, 9)))
        self.assertEqual(
            self.calls,
            [
                ("/render", {"model": "stub", "messages": MESSAGES, "tools": body["tools"]}),
                ("/tokenize", {"model": "stub", "prompt": RENDERED, "add_special_tokens": False}),
            ],
        )

    async def test_a_text_only_dialect_shapes_both_tokenize_requests(self):
        self.client = self.engine(TextOnly())
        self.responses["/tokenize"] = httpx.Response(200, json={"tokens": [1, 7, 9, 4]})
        result = await self.tokenize({**CHAT, "add_special_tokens": False})
        self.assertEqual((result.count, result.token_ids), (4, (1, 7, 9, 4)))
        self.assertEqual(self.calls[-1], ("/tokenize", {"content": RENDERED, "add_special": False}))
        self.calls.clear()
        await self.tokenize({"model": "stub", "prompt": "hello"})
        self.assertEqual(self.calls, [("/tokenize", {"content": "hello", "add_special": True})])

    async def test_a_failed_render_gives_no_count_and_stops_before_the_tokenize_route(self):
        for response, failure in (
            (httpx.Response(503, text="loading"), (503, "loading")),
            (httpx.Response(200, text="{"), (502, "invalid JSON response")),
            (httpx.Response(200, json=[]), (502, "/render gave no rendered prompt")),
            (httpx.Response(200, json={"prompt": 3}), (502, "/render gave no rendered prompt")),
            (httpx.ConnectError("refused"), (502, None)),
        ):
            with self.subTest(failure=failure):
                self.responses["/render"] = response
                self.calls.clear()
                status, detail = await self.failure(CHAT)
                self.assertEqual((status, failure[1] or detail), (failure[0], detail))
                self.assertEqual(self.paths(), ["/render", "/render"])

    async def test_a_failed_count_after_a_render_gives_no_count(self):
        for response, failure in (
            (httpx.Response(500, text="busy"), (500, "busy")),
            (httpx.Response(200, text="{"), (502, "invalid JSON response")),
            (httpx.Response(200, json={}), (502, "response has no valid token count")),
        ):
            with self.subTest(failure=failure):
                self.responses["/tokenize"] = response
                self.calls.clear()
                self.assertEqual(await self.failure(CHAT), failure)
                self.assertEqual(self.paths(), ["/render", "/tokenize"] * 2)

    async def test_a_dialect_with_no_render_route_reads_a_reply_that_is_no_object(self):
        self.client = self.engine(Base())
        self.responses["/tokenize"] = httpx.Response(200, json=[])
        self.assertEqual(await self.failure(CHAT), (502, "response has no valid token count"))
        self.assertEqual(self.paths(), ["/tokenize", "/tokenize"])
        self.assertEqual(self.calls[0][1], {"model": "stub", "messages": MESSAGES})

    async def test_one_deadline_covers_the_render_and_the_count(self):
        self.delay = 0.06
        self.assertIsNone(await self.tokenize(CHAT, timeout_s=0.1))
        self.assertEqual(self.paths(), ["/render", "/tokenize"])
        with self.assertRaisesRegex(EngineError, "exact count exceeded 0.1s"):
            await self.tokenize(CHAT, strict=True, timeout_s=0.1)
        self.assertEqual((await self.tokenize(CHAT, timeout_s=1)).count, 3)
