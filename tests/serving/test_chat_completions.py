"""Exercise endpoint-aware response assembly through HTTP and split engine legs."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from narwhal.config import SLO, EngineSpec, FleetConfig
from narwhal.profiling.model import Profile
from narwhal.serving.app import create_app
from narwhal.serving.router import NarwhalRouter
from narwhal.types import Role


def chunk(delta, *, token=None, finish=None, **fields):
    choice = {"index": 0, "delta": delta, "finish_reason": finish, **fields}
    if token is not None:
        choice["token_ids"] = [token]
    return {"choices": [choice]}


class ChatCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        cfg = FleetConfig(
            model="stub",
            slo=SLO(ttft_s=10.0, tpot_s=0.5),
            engines=[
                EngineSpec("p", "http://prefill", Role.PREFILL),
                EngineSpec("d", "http://decode", Role.DECODE),
            ],
            profiles_path=Path(self.directory.name) / "profiles.json",
            tokenize=False,
            admission="open",
        )
        self.calls = []
        self.frames = [chunk({"role": "assistant"}), chunk({"content": "Hi"}, token=1)]

        def engine(request):
            body = json.loads(request.content)
            self.calls.append((request.url.path, body))
            if (body.get("kv_transfer_params") or {}).get("do_remote_decode"):
                return httpx.Response(
                    200,
                    json={"kv_transfer_params": {"remote_engine_id": "p", "remote_block_ids": [1]}},
                )
            self.assertTrue(body["stream"])
            self.assertTrue(body["return_token_ids"])
            self.assertEqual(body["stream_interval"], 1)
            frames = [
                {
                    "id": "chat-fixture",
                    "object": "chat.completion.chunk",
                    "created": 123,
                    "model": "stub",
                    "prompt_token_ids": [10, 11],
                    "choices": [],
                },
                *self.frames,
            ]
            wire = "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames)
            return httpx.Response(
                200, text=wire + "data: [DONE]\n\n", headers={"content-type": "text/event-stream"}
            )

        def router(*args, **kwargs):
            return NarwhalRouter(*args, transport=httpx.MockTransport(engine), **kwargs)

        with patch("narwhal.serving.app.NarwhalRouter", side_effect=router):
            self.app = create_app(cfg)
        self.router = self.app.state.router
        self.addAsyncCleanup(self.router.engines.aclose)
        for iid in ("p", "d"):
            self.router.profiles.put(
                Profile(
                    iid,
                    2e-8,
                    6e-5,
                    0.005,
                    3e-6,
                    0.012,
                    decode_min_requests=1,
                    decode_max_requests=4,
                    decode_min_kv_tokens=1,
                    decode_max_kv_tokens=1024,
                    decode_fit_mape=0.0,
                    decode_cv_mape=0.0,
                )
            )
        self.router.journal.open()
        self.addCleanup(self.router.journal.close)
        # No monitoring lifespan or network fleet is needed for HTTP serving.
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://router"
        )
        self.addAsyncCleanup(self.client.aclose)

    async def post(self, *, endpoint="/v1/chat/completions", **fields):
        body = {"model": "stub", "max_tokens": 16, **fields}
        body["messages" if endpoint.endswith("chat/completions") else "prompt"] = (
            [{"role": "user", "content": "hello"}]
            if endpoint.endswith("chat/completions")
            else "hello"
        )
        response = await self.client.post(endpoint, json=body)
        self.assertEqual(self.router.inflight, 0)
        self.assertEqual(self.router.ingress_inflight, 0)
        self.assertFalse(self.router.monitor.waiting)
        return response

    async def test_chat_message_metadata_usage_and_token_exposure(self):
        self.frames = [
            chunk({"role": "assistant", "content": ""}),
            chunk({"content": "Hello "}, token=1),
            chunk({"content": "world"}, token=2, finish="length"),
            {
                "choices": [],
                "usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
            },
        ]
        for expose in (False, True):
            with self.subTest(expose=expose):
                response = await self.post(return_token_ids=expose)
                self.assertEqual(response.status_code, 200, response.text)
                out = response.json()
                self.assertEqual(out["object"], "chat.completion")
                self.assertEqual(
                    (out["id"], out["created"], out["model"]), ("chat-fixture", 123, "stub")
                )
                choice = out["choices"][0]
                self.assertEqual(choice["message"], {"role": "assistant", "content": "Hello world"})
                self.assertNotIn("text", choice)
                self.assertEqual(choice["finish_reason"], "length")
                self.assertEqual(out["usage"]["total_tokens"], 4)
                self.assertEqual(choice.get("token_ids"), [1, 2] if expose else None)
                self.assertEqual(out.get("prompt_token_ids"), [10, 11] if expose else None)
        self.assertEqual(self.router.served, 2)
        self.assertEqual(self.router.failed, 0)
        self.assertTrue(all(path == "/v1/chat/completions" for path, _ in self.calls))

    async def test_fragmented_interleaved_function_tools(self):
        self.frames = [
            chunk(
                {
                    "tool_calls": [
                        {
                            "index": 1,
                            "id": "call_",
                            "type": "function",
                            "function": {"name": "get_", "arguments": '{"x":'},
                        }
                    ]
                },
                token=1,
            ),
            chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "first",
                            "type": "function",
                            "function": {"name": "ping", "arguments": "{}"},
                        }
                    ]
                },
                token=2,
            ),
            chunk(
                {
                    "tool_calls": [
                        {
                            "index": 1,
                            "id": "second",
                            "function": {"name": "value", "arguments": "1}"},
                        }
                    ]
                },
                token=3,
            ),
            chunk({}, finish="tool_calls"),
            {"choices": [], "usage": None},
        ]
        response = await self.post(tools=[{"type": "function", "function": {"name": "get_value"}}])
        self.assertEqual(response.status_code, 200, response.text)
        out = response.json()
        self.assertEqual(
            out["choices"][0]["message"],
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "first",
                        "type": "function",
                        "function": {"name": "ping", "arguments": "{}"},
                    },
                    {
                        "id": "call_second",
                        "type": "function",
                        "function": {"name": "get_value", "arguments": '{"x":1}'},
                    },
                ],
            },
        )
        self.assertEqual(out["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(out["usage"]["completion_tokens"], 3)
        self.assertEqual(self.calls[0][1]["tools"], self.calls[1][1]["tools"])

    async def test_reasoning_refusal_and_legacy_function_fields(self):
        for field in ("reasoning", "reasoning_content", "refusal"):
            with self.subTest(field=field):
                self.frames = [chunk({field: "part "}, token=1), chunk({field: "two"}, token=2)]
                response = await self.post()
                self.assertEqual(response.status_code, 200, response.text)
                message = response.json()["choices"][0]["message"]
                self.assertEqual(message[field], "part two")
                self.assertIsNone(message["content"])
        self.frames = [
            chunk({"function_call": {"name": "get_", "arguments": "{"}}, token=1),
            chunk(
                {"function_call": {"name": "value", "arguments": "}"}},
                token=2,
                finish="function_call",
            ),
        ]
        response = await self.post()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json()["choices"][0]["message"]["function_call"],
            {"name": "get_value", "arguments": "{}"},
        )

    async def test_text_completions_and_logprobs_keep_their_shape(self):
        self.frames = [
            {
                "choices": [
                    {
                        "index": 0,
                        "text": text,
                        "token_ids": [token],
                        "logprobs": {
                            "tokens": [text],
                            "token_logprobs": [-0.1],
                            "top_logprobs": [None],
                            "text_offset": [offset],
                        },
                        "finish_reason": "length" if token == 2 else None,
                    }
                ]
            }
            for text, token, offset in (("Hello ", 1, 0), ("world", 2, 6))
        ]
        self.frames[0]["choices"][0]["prompt_token_ids"] = [10, 11]
        self.frames[1]["choices"][0]["prompt_token_ids"] = None
        response = await self.post(endpoint="/v1/completions", return_token_ids=True, logprobs=1)
        self.assertEqual(response.status_code, 200, response.text)
        out = response.json()
        self.assertEqual(out["object"], "text_completion")
        choice = out["choices"][0]
        self.assertEqual(choice["text"], "Hello world")
        self.assertNotIn("message", choice)
        self.assertEqual(choice["token_ids"], [1, 2])
        self.assertEqual(choice["prompt_token_ids"], [10, 11])
        self.assertEqual(choice["finish_reason"], "length")
        self.assertEqual(
            choice["logprobs"],
            {
                "tokens": ["Hello ", "world"],
                "token_logprobs": [-0.1, -0.1],
                "top_logprobs": [None, None],
                "text_offset": [0, 6],
            },
        )
        streamed = await self.post(endpoint="/v1/completions", stream=True)
        self.assertEqual(streamed.status_code, 200, streamed.text)
        rows = [
            json.loads(line[5:])
            for line in streamed.text.splitlines()
            if line.startswith("data:") and "[DONE]" not in line
        ]
        self.assertEqual("".join(row["choices"][0]["text"] for row in rows[1:]), "Hello world")
        self.assertTrue(streamed.text.endswith("data: [DONE]\n\n"))
        self.assertNotIn("prompt_token_ids", rows[1]["choices"][0])

    async def test_chat_logprobs_and_stop_reason(self):
        scores = [
            {"token": text, "logprob": -0.1, "bytes": None, "top_logprobs": []}
            for text in ("a", "b")
        ]
        self.frames = [
            chunk(
                {"content": score["token"]}, token=i, logprobs={"content": [score], "refusal": None}
            )
            for i, score in enumerate(scores)
        ]
        self.frames.extend([chunk({}, finish="stop", stop_reason=0), {"choices": []}])
        response = await self.post(logprobs=True)
        self.assertEqual(response.status_code, 200, response.text)
        choice = response.json()["choices"][0]
        self.assertEqual(choice["logprobs"], {"content": scores, "refusal": None})
        self.assertEqual(choice["stop_reason"], 0)

    async def test_streaming_preserves_deltas_and_requested_identity(self):
        self.frames = [
            chunk({"role": "assistant"}),
            chunk({"reasoning": "think"}, token=1),
            chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "ping", "arguments": "{}"},
                        }
                    ]
                },
                token=2,
                finish="tool_calls",
            ),
        ]
        for expose in (False, True):
            with self.subTest(expose=expose):
                response = await self.post(stream=True, return_token_ids=expose)
                self.assertEqual(response.status_code, 200, response.text)
                lines = [
                    line[5:].strip()
                    for line in response.text.splitlines()
                    if line.startswith("data:")
                ]
                self.assertEqual(lines[-1], "[DONE]")
                rows = [json.loads(line) for line in lines[:-1]]
                self.assertEqual(
                    [row["choices"][0]["delta"] for row in rows[1:]],
                    [row["choices"][0]["delta"] for row in self.frames],
                )
                self.assertEqual(rows[-1]["choices"][0].get("token_ids"), [2] if expose else None)
        self.assertEqual(self.router.served, 2)

    async def test_unsupported_requests_reject_before_engine_work(self):
        for fields, param in (
            ({"audio": {"format": "wav"}}, "audio"),
            ({"modalities": ["text", "audio"]}, "modalities"),
            ({"tools": [{"type": "custom"}]}, "tools"),
        ):
            with self.subTest(fields=fields):
                response = await self.post(**fields)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(response.json()["error"]["param"], param)
        self.assertFalse(self.calls)
        self.assertEqual(self.router.invalid_requests, 3)

    async def test_upstream_error_cannot_be_folded_into_success(self):
        self.frames = [
            chunk({"content": "partial"}, token=1),
            {"error": {"message": "fixture failure", "code": 500}},
        ]
        response = await self.post()
        self.assertEqual(response.status_code, 502, response.text)
        self.assertIn("error", response.json())
        self.assertNotIn("choices", response.json())
        self.assertEqual(self.router.served, 0)
        self.assertEqual(self.router.failed, 1)

    async def test_unsupported_or_malformed_output_is_failed_not_served(self):
        for delta in (
            {"audio": {"data": "private-output"}},
            {"content": ["bad"]},
            {"tool_calls": [{"index": 0, "type": "custom"}]},
            {"tool_calls": [{"index": 0, "function": {"arguments": "{}"}}]},
        ):
            with self.subTest(delta=delta):
                self.frames = [chunk(delta, token=1)]
                response = await self.post()
                self.assertEqual(response.status_code, 502, response.text)
                self.assertNotIn("private-output", response.text)
                self.assertIn("non-streaming", response.json()["error"]["message"])
        self.assertEqual(self.router.served, 0)
        self.assertEqual(self.router.failed, 4)


if __name__ == "__main__":
    unittest.main()
