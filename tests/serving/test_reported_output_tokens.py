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
        self.steps: list[tuple[str, str]] = []
        self.router, self.client = serving_app(self, self.engine, backend="llamacpp")

    def engine(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        if request.url.path == "/props":
            return httpx.Response(200, json={"total_slots": 2})
        if request.url.path.startswith("/slots/"):
            self.steps.append((request.url.host, request.url.params["action"]))
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

    def row(self) -> dict:
        lines = self.router.journal.path.read_text().splitlines()
        return next(row for line in reversed(lines) if "rid" in (row := json.loads(line)))

    async def test_the_reported_count_sets_the_output_length_and_the_token_interval(self):
        # One chunk of text holds the five tokens the stream reports.
        self.chunks = ["abcde"]
        reply = (await self.complete()).json()
        self.assertEqual(reply["usage"]["completion_tokens"], 5)
        self.assertEqual(self.router.tpot.n, 1)
        self.assertEqual(self.router.state()["token_accounting"], "reported")
        self.assertEqual(self.router.state()["serving"]["output_tokens"], 5)
        metrics = (await self.client.get("/metrics")).text
        self.assertIn("\nnarwhal_output_tokens_total 5\n", metrics)
        row = self.row()
        self.assertEqual((row["output_len"], row["decode_tokens_observed"]), (5, None))
        self.assertGreaterEqual(row["tpot_s"], 0)
        self.assertEqual(row["token_accounting"], "reported")

    async def test_a_one_token_reply_in_several_chunks_has_no_token_interval(self):
        self.reported = 1
        await self.complete()
        self.assertEqual(self.router.tpot.n, 0)
        row = self.row()
        self.assertEqual((row["output_len"], row["tpot_s"]), (1, None))

    async def test_each_leg_ran_its_slot_step(self):
        await self.complete()
        self.assertEqual(self.steps, [("prefill", "save"), ("decode", "restore")])

    async def test_a_stream_that_reports_no_count_leaves_the_output_length_unknown(self):
        self.reported = None
        reply = (await self.complete()).json()
        self.assertEqual(reply["choices"][0]["text"], "abc")
        self.assertNotIn("usage", reply)
        self.assertEqual(self.router.tpot.n, 0)
        self.assertEqual(self.router.state()["serving"]["output_tokens"], 0)

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
