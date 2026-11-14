"""A routed request runs a staged connector's steps and frees its export when the request ends."""

import json
import unittest

import httpx

from tests.engines.test_staged_handoff import Staged
from tests.wire import serving_app

FRAME = {"id": "c", "object": "text_completion", "model": "stub"}


class StagedServingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.requests: list[tuple[str, str]] = []
        self.decode_failures = 0
        self.router, self.client = serving_app(self, self.engine)
        self.kv = Staged()
        self.router.engines.kv = self.kv

    def engine(self, request):
        body = json.loads(request.content) if request.content else {}
        action = request.url.params.get("action", "")
        kind = action or ("decode" if body.get("stream") else "prefill")
        self.requests.append((request.url.host, kind))
        if action:
            return httpx.Response(200, json={})
        if kind == "prefill":
            return httpx.Response(200, json={"choices": [{"text": "x"}]})
        if self.decode_failures:
            self.decode_failures -= 1
            return httpx.Response(500, text="decode failed")
        frames = [
            {**FRAME, "choices": [{"index": 0, "text": "a", "token_ids": [1]}]},
            {
                **FRAME,
                "choices": [{"index": 0, "text": "b", "token_ids": [2], "finish_reason": "length"}],
            },
        ]
        wire = "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames)
        return httpx.Response(
            200, text=wire + "data: [DONE]\n\n", headers={"content-type": "text/event-stream"}
        )

    async def post(self):
        return await self.client.post(
            "/v1/completions",
            json={"model": "stub", "prompt": "hello", "max_tokens": 2, "stream": True},
        )

    async def test_a_request_exports_restores_and_frees_the_export(self):
        response = await self.post()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('"text":"a"', response.text)
        self.assertEqual(
            self.requests,
            [
                ("prefill", "prefill"),
                ("prefill", "save"),
                ("decode", "restore"),
                ("decode", "decode"),
            ],
        )
        self.assertEqual(self.kv.held, set())
        self.assertEqual(
            [e for e in self.kv.events if e[0] == "discard"], [("discard", "prefill-3.bin")]
        )

    async def test_a_failed_decode_attempt_frees_its_export(self):
        self.decode_failures = 1
        await self.post()
        self.assertEqual(self.kv.held, set())
        exports = [e for e in self.kv.events if e[0] == "export"]
        discards = [e for e in self.kv.events if e[0] == "discard"]
        self.assertEqual(len(discards), len(exports))


if __name__ == "__main__":
    unittest.main()
