"""A routed reply's output length comes from the count the engine's stream reports."""

import json
import unittest

import httpx

from narwhal.backends import load
from narwhal.engines.stream import parse_event, sse_token_bearing
from tests.wire import serving_app

FRAME = {"id": "c", "object": "text_completion", "model": "stub"}
FINISH = {"index": 0, "text": "", "finish_reason": "length"}


def stream(*frames: dict) -> httpx.Response:
    wire = "".join("data: " + json.dumps({**FRAME, **frame}) + "\n\n" for frame in frames)
    return httpx.Response(
        200, text=wire + "data: [DONE]\n\n", headers={"content-type": "text/event-stream"}
    )


class ReportedOutputTokenTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Text chunks of the decode stream, and the count its last chunk reports.
        self.chunks, self.reported = ["a", "b", "c"], 5
        self.router, self.client = serving_app(self, self.engine, backend="llamacpp")

    def engine(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        if request.url.path == "/props":
            return httpx.Response(200, json={"total_slots": 2})
        if request.url.path.startswith("/slots/"):
            return httpx.Response(200, json={"n_saved": 3})
        if not body.get("stream"):
            return httpx.Response(200, json={"choices": [{"text": "x"}]})
        last = dict(FINISH)
        timings = {} if self.reported is None else {"timings": {"predicted_n": self.reported}}
        return stream(
            *({"choices": [{"index": 0, "text": text}]} for text in self.chunks),
            {"choices": [last], **timings},
        )

    async def complete(self, **fields) -> httpx.Response:
        body = {"model": "stub", "prompt": "hello", "max_tokens": 8, **fields}
        response = await self.client.post("/v1/completions", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response

    async def test_the_reported_count_sets_the_output_length_and_the_token_interval(self):
        reply = (await self.complete()).json()
        self.assertEqual(reply["choices"][0]["text"], "abc")
        self.assertEqual(reply["usage"]["completion_tokens"], 5)
        self.assertEqual(self.router.tpot.n, 1)

    async def test_a_stream_that_reports_no_count_leaves_the_output_length_unknown(self):
        self.reported = None
        reply = (await self.complete()).json()
        self.assertEqual(reply["choices"][0]["text"], "abc")
        self.assertNotIn("usage", reply)
        self.assertEqual(self.router.tpot.n, 0)

    async def test_a_reply_of_no_text_is_served(self):
        self.chunks, self.reported = [], 1
        reply = (await self.complete()).json()
        self.assertEqual(reply["choices"][0]["text"], "")
        self.assertEqual(reply["choices"][0]["finish_reason"], "length")
        streamed = await self.complete(stream=True)
        self.assertIn('"finish_reason":"length"', streamed.text)
        self.assertIn("data: [DONE]", streamed.text)
        self.assertEqual(self.router.failed, 0)

    def test_a_finish_frame_is_output_only_without_token_ids(self):
        finish = parse_event("data: " + json.dumps({"choices": [FINISH]}))
        self.assertTrue(sse_token_bearing(finish, load("llamacpp").dialect))
        for name in ("vllm", "sglang"):
            dialect = load(name).dialect
            self.assertFalse(sse_token_bearing(finish, dialect, "/v1/chat/completions"), name)

    def test_the_count_is_the_generated_tokens_of_the_final_timings(self):
        dialect = load("llamacpp").dialect
        self.assertEqual(dialect.stream_output_tokens({"timings": {"predicted_n": 40}}), 40)
        for chunk in ({}, {"timings": {}}, {"timings": {"predicted_n": "40"}}, {"timings": 3}):
            self.assertIsNone(dialect.stream_output_tokens(chunk))
        self.assertIsNone(
            load("vllm").dialect.stream_output_tokens({"timings": {"predicted_n": 4}})
        )
