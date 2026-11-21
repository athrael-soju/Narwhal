"""Streamed completions and chat completions relay the decode engine's final usage frame."""

import json
import unittest

import httpx

from tests.wire import serving_app

PROMPT_IDS = [10, 11, 12]


class UsageFrameTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []

        def engine(request):
            body = json.loads(request.content) if request.content else None
            self.calls.append({"engine": request.url.host, "path": request.url.path, "body": body})
            if request.url.path == "/tokenize":
                return httpx.Response(
                    200, json={"count": 3, "max_model_len": 4096, "tokens": PROMPT_IDS}
                )
            if (body.get("kv_transfer_params") or {}).get("do_remote_decode"):
                return httpx.Response(
                    200,
                    json={"kv_transfer_params": {"remote_engine_id": "p", "remote_block_ids": [1]}},
                )
            chat = request.url.path.endswith("chat/completions")
            tokens = body["max_tokens"]
            frames = [{"id": "x", "prompt_token_ids": PROMPT_IDS, "choices": []}]
            for index in range(tokens):
                last = index == tokens - 1
                choice = (
                    {"index": 0, "delta": {"content": "a"}, "token_ids": [100 + index]}
                    if chat
                    else {"index": 0, "text": "a", "token_ids": [100 + index]}
                )
                choice["finish_reason"] = "length" if last else None
                frames.append({"id": "x", "model": "stub", "choices": [choice]})
            if (body.get("stream_options") or {}).get("include_usage"):
                frames.append(
                    {
                        "id": "x",
                        "model": "stub",
                        "choices": [],
                        "usage": {
                            "prompt_tokens": len(PROMPT_IDS),
                            "completion_tokens": tokens,
                            "total_tokens": len(PROMPT_IDS) + tokens,
                        },
                    }
                )
            wire = "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames)
            return httpx.Response(
                200, text=wire + "data: [DONE]\n\n", headers={"content-type": "text/event-stream"}
            )

        self.router, self.client = serving_app(
            self, engine, urls=("http://stub-0", "http://stub-1"), tokenize=True
        )

    async def stream(self, endpoint, **fields):
        body = {"model": "stub", "stream": True, "max_tokens": 6, "ignore_eos": True, **fields}
        if endpoint.endswith("chat/completions"):
            body["messages"] = [{"role": "user", "content": "hello"}]
        else:
            body["prompt"] = "hello"
        response = await self.client.post(endpoint, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.router.inflight, 0)
        lines = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
        self.assertEqual(lines[-1], "[DONE]")
        return [json.loads(line) for line in lines[:-1]]

    def engine_bodies(self, endpoint):
        return [c["body"] for c in self.calls if c["path"] == endpoint]

    async def test_the_final_usage_frame_counts_every_requested_token_after_a_handoff(self):
        for endpoint in ("/v1/completions", "/v1/chat/completions"):
            with self.subTest(endpoint=endpoint):
                self.calls.clear()
                frames = await self.stream(endpoint, stream_options={"include_usage": True})
                usage = [frame for frame in frames if frame.get("usage")]
                self.assertEqual(len(usage), 1, frames)
                self.assertIs(frames[-1], usage[0])
                self.assertEqual(usage[0]["choices"], [])
                self.assertEqual(
                    usage[0]["usage"],
                    {"prompt_tokens": 3, "completion_tokens": 6, "total_tokens": 9},
                )
                prefill, decode = self.engine_bodies(endpoint)
                self.assertNotEqual(self.calls[1]["engine"], self.calls[2]["engine"])
                self.assertEqual(prefill["max_tokens"], 1)
                self.assertNotIn("stream_options", prefill)
                self.assertEqual(decode["stream_options"], {"include_usage": True})
                self.assertIs(decode["ignore_eos"], True)
                self.assertEqual(decode["max_tokens"], 6)

    async def test_without_include_usage_no_usage_frame_reaches_the_client(self):
        for endpoint in ("/v1/completions", "/v1/chat/completions"):
            with self.subTest(endpoint=endpoint):
                self.calls.clear()
                frames = await self.stream(endpoint)
                self.assertFalse([frame for frame in frames if frame.get("usage")], frames)
                self.assertIs(self.engine_bodies(endpoint)[1]["ignore_eos"], True)

    async def test_streamed_token_frames_add_up_to_the_requested_length(self):
        for endpoint in ("/v1/completions", "/v1/chat/completions"):
            with self.subTest(endpoint=endpoint):
                frames = await self.stream(endpoint, stream_options={"include_usage": True})
                pieces = [
                    choice.get("text") or (choice.get("delta") or {}).get("content")
                    for frame in frames
                    for choice in frame["choices"]
                ]
                self.assertEqual("".join(p for p in pieces if p), "a" * 6)
                finish = [
                    c["finish_reason"]
                    for f in frames
                    for c in f["choices"]
                    if c.get("finish_reason")
                ]
                self.assertEqual(finish, ["length"])


if __name__ == "__main__":
    unittest.main()
