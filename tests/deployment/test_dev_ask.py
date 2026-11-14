import asyncio
import itertools
import json
from pathlib import Path
from unittest.mock import patch

import httpx

from narwhal.dev import ask, cli, lifecycle
from tests.deployment.test_dev_direct import DirectInstance


def chat_stream(texts: list[str], tokens: int | None, rid: str = "rid-1") -> httpx.Response:
    frames = [{"choices": [{"index": 0, "delta": {"content": text}}]} for text in texts]
    frames.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
    if tokens is not None:
        frames.append({"choices": [], "usage": {"completion_tokens": tokens}})
    wire = "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames)
    return httpx.Response(
        200,
        text=wire + "data: [DONE]\n\n",
        headers={"content-type": "text/event-stream", "x-request-id": rid},
    )


class AskTests(DirectInstance):
    def setUp(self):
        super().setUp()
        self.init()
        self.run_directory = self.root / "run-1"
        self.run_directory.mkdir()
        lifecycle.write(self.run_directory / "fleet.json", {"model": "served"})
        self.state("launched")
        self.bodies: list[dict] = []
        self.replies: list[httpx.Response] = []

    def state(self, phase: str) -> None:
        document = {"run": str(self.run_directory), "phase": phase, "processes": []}
        lifecycle.write(self.root / "lifecycle.json", document)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        return self.replies.pop(0) if self.replies else chat_stream(["Tok", "yo"], 2)

    def ask(self, **settings) -> dict:
        return ask.run(self.root, transport=httpx.MockTransport(self.handle), **settings)

    def test_one_prompt_reports_the_reply_its_timing_and_the_engines_of_each_leg(self):
        rows = [
            {"event": "controller_decision"},
            {"rid": "rid-1", "prefill_iid": "n1", "decode_iid": "n3"},
        ]
        (self.run_directory / "journal.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows)
        )
        result = self.ask(prompt="What is the capital of Japan?", max_tokens=16)
        self.assertEqual(
            (result["status"], result["reply"], result["output_tokens"]), ("answered", "Tokyo", 2)
        )
        self.assertEqual(result["engines"], {"prefill": "n1", "decode": "n3"})
        self.assertGreaterEqual(result["ttft_s"], 0)
        [body] = self.bodies
        self.assertEqual(
            body,
            {
                "chat_template_kwargs": {"enable_thinking": False},
                "model": "served",
                "messages": [{"role": "user", "content": "What is the capital of Japan?"}],
                "max_tokens": 16,
                "stream": True,
                "stream_options": {"include_usage": True},
            },
        )

    def test_the_client_times_the_first_output_and_the_interval_between_tokens(self):
        ticks = itertools.count(step=0.5)

        async def timed() -> ask.Answer:
            transport = httpx.MockTransport(lambda request: chat_stream(["a", "b", "c"], 5))
            async with httpx.AsyncClient(transport=transport) as client:
                return await ask.ask(client, "http://router", {}, clock=lambda: next(ticks))

        answer = asyncio.run(timed())
        # The clock reads 0 at the start, then 0.5, 1.0 and 1.5 at the three chunks of text.
        self.assertEqual((answer.ttft_s, answer.tpot_s, answer.output_tokens), (0.5, 0.25, 5))
        self.assertEqual(answer.text, "abc")

    def test_many_prompts_report_the_spread_the_throughput_and_the_failures(self):
        self.replies = [httpx.Response(503, text="busy"), chat_stream(["x"], None)]
        result = self.ask(repeat=14, concurrency=4)
        self.assertEqual(
            (result["status"], result["requests"], result["failed"], result["concurrency"]),
            ("completed", 14, 1, 4),
        )
        self.assertEqual(result["errors"], ["HTTP 503: busy"])
        self.assertEqual(set(result["ttft_s"]), {"p50", "p95"})
        self.assertGreater(result["output_tokens_per_second"], 0)
        prompts = [body["messages"][0]["content"] for body in self.bodies]
        self.assertEqual(sorted(prompts), sorted([*ask.PROMPTS, *ask.PROMPTS[:2]]))
        with patch.object(ask, "run", return_value=result):
            arguments = ["dev", "ask", "--instance", str(self.root), "--repeat", "14"]
            self.assertEqual(cli._main(arguments), 1)

    def test_the_spread_uses_nearest_rank_percentiles(self):
        values = [float(value) for value in range(1, 21)]
        self.assertEqual((ask.percentile(values, 0.5), ask.percentile(values, 0.95)), (10.0, 19.0))
        self.assertEqual(ask.percentile([3.0], 0.95), 3.0)
        self.assertIsNone(ask.percentile([], 0.5))

    def test_ask_needs_a_launched_instance_and_one_kind_of_request(self):
        for settings, message in (
            ({}, "give one prompt, or --repeat"),
            ({"prompt": "x", "repeat": 2}, "give one prompt, or --repeat"),
            ({"repeat": 0}, "must be positive"),
            ({"prompt": "x", "max_tokens": 0}, "must be positive"),
        ):
            with self.subTest(settings=settings), self.assertRaisesRegex(ValueError, message):
                self.ask(**settings)
        self.state("stopped")
        with self.assertRaisesRegex(ValueError, "requires a launched instance"):
            self.ask(prompt="x")

    def test_a_failed_single_request_fails_the_command(self):
        self.replies = [httpx.Response(429, text="refused")]
        with self.assertRaisesRegex(lifecycle.LifecycleOperationError, "HTTP 429: refused"):
            self.ask(prompt="x")
        self.assertEqual(ask._legs(Path("/nonexistent"), None), {"prefill": None, "decode": None})
