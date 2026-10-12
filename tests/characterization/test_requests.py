import json
import re
import unittest

import httpx

from narwhal.serving.handoff import handoff_bound
from tests.backends.fixtures.vllm import launch, nixl_config
from tests.characterization.golden import assert_golden
from tests.wire import serving_app

PROMPT_IDS = [10, 11, 12]
REQUEST_ID = re.compile(r"^(.+)-a(\d+)-(prefill|decode)$")


def chunk(choice, **fields):
    return {"id": "fixture-id", "created": 123, "model": "stub", "choices": [choice], **fields}


class RouterWireTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.prefill_payload = {
            "kv_transfer_params": {"remote_engine_id": "p", "remote_block_ids": [1, 2]}
        }

        def engine(request):
            body = json.loads(request.content) if request.content else None
            self.calls.append(
                {
                    "engine": request.url.host,
                    "method": request.method,
                    "path": request.url.path,
                    "x-request-id": request.headers.get("x-request-id"),
                    "body": body,
                }
            )
            if request.url.path == "/tokenize":
                return httpx.Response(
                    200,
                    json={"count": 3, "max_model_len": 4096, "tokens": PROMPT_IDS},
                )
            if (body.get("kv_transfer_params") or {}).get("do_remote_decode"):
                return httpx.Response(200, json=self.prefill_payload)
            chat = request.url.path.endswith("chat/completions")
            frames = [
                {"id": "fixture-id", "prompt_token_ids": PROMPT_IDS, "choices": []},
                chunk(
                    {"index": 0, "delta": {"role": "assistant", "content": "Hi"}, "token_ids": [7]}
                    if chat
                    else {"index": 0, "text": "Hi", "token_ids": [7]}
                ),
                chunk(
                    {"index": 0, "delta": {"content": "!"}, "token_ids": [8]}
                    if chat
                    else {"index": 0, "text": "!", "token_ids": [8]},
                    finish_reason=None,
                ),
                chunk(
                    {"index": 0, "delta": {}, "finish_reason": "length", "token_ids": []}
                    if chat
                    else {"index": 0, "text": "", "finish_reason": "length", "token_ids": []}
                ),
            ]
            wire = "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames)
            return httpx.Response(
                200, text=wire + "data: [DONE]\n\n", headers={"content-type": "text/event-stream"}
            )

        self.router, self.client = serving_app(
            self, engine, urls=("http://stub-0", "http://stub-1"), tokenize=True
        )

    def record(self, response):
        replacements = {}
        for call in self.calls:
            match = REQUEST_ID.match(call["x-request-id"] or "")
            if match:
                replacements[match.group(1)] = "<rid>"
        text = response.text
        try:
            client = response.json()
        except ValueError:
            client = text.split("\n")
        return {
            "engine_calls": self.calls,
            "client_status": response.status_code,
            "client_body": client,
        }, replacements

    async def serve(self, endpoint, body):
        response = await self.client.post(endpoint, json={"model": "stub", **body})
        self.assertEqual(self.router.inflight, 0)
        return response

    async def test_chat_non_streaming_cross_engine(self):
        response = await self.serve(
            "/v1/chat/completions",
            {
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 4,
                "max_completion_tokens": 4,
                "min_tokens": 1,
                "n": 1,
                "chat_template_kwargs": {"enable_thinking": False},
                "add_generation_prompt": True,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        assert_golden(self, "requests_chat_non_streaming", *self.record(response))

    async def test_completions_streaming_cross_engine(self):
        response = await self.serve(
            "/v1/completions",
            {
                "prompt": "hello",
                "max_tokens": 4,
                "stream": True,
                "stream_options": {"include_usage": True},
                "best_of": 1,
                "add_special_tokens": False,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        assert_golden(self, "requests_completions_streaming", *self.record(response))

    async def test_completions_streaming_exposes_requested_token_ids(self):
        response = await self.serve(
            "/v1/completions",
            {"prompt": [1, 2, 3], "max_tokens": 4, "stream": True, "return_token_ids": True},
        )
        self.assertEqual(response.status_code, 200, response.text)
        assert_golden(self, "requests_completions_token_ids", *self.record(response))

    async def test_client_kv_transfer_params_never_reach_an_engine(self):
        response = await self.serve(
            "/v1/completions",
            {
                "prompt": "hello",
                "max_tokens": 4,
                "kv_transfer_params": {"remote_engine_id": "spoofed", "remote_block_ids": [9]},
            },
        )
        assert_golden(self, "requests_client_kv_transfer_params", *self.record(response))

    async def test_reserved_vllm_xargs_are_rejected(self):
        record = {}
        for key in ("kv_cache_report_mode", "kv_transfer_params", "ec_transfer_params"):
            response = await self.serve(
                "/v1/completions",
                {"prompt": "hello", "max_tokens": 4, "vllm_xargs": {key: "x"}},
            )
            record[key] = {"status": response.status_code, "body": response.json()}
        record["engine_calls"] = self.calls
        assert_golden(self, "requests_reserved_vllm_xargs", record)

    async def test_prefill_descriptors_and_decode_continuations(self):
        engines = self.router.engines
        body = {"model": "stub", "prompt": "hello", "max_tokens": 4, "stream": False}
        record = {}
        for name, payload in (
            ("flat", {"kv_transfer_params": {"remote_engine_id": "p", "remote_block_ids": [1]}}),
            (
                "grouped",
                {"kv_transfer_params": {"remote_engine_id": "p", "remote_block_ids": [[1], [2]]}},
            ),
            (
                "in_choice",
                {
                    "choices": [
                        {"kv_transfer_params": {"remote_engine_id": "p", "remote_block_ids": [3]}}
                    ]
                },
            ),
        ):
            self.prefill_payload = payload
            self.calls.clear()
            result = await engines.prefill(
                "http://stub-0", "/v1/completions", body, {}, request_id="fixture-a1-prefill"
            )
            legs = {}
            for target in ("http://stub-1", "http://stub-0"):
                stream = engines.decode(
                    target,
                    "/v1/completions",
                    {**body, "kv_transfer_params": {"spoofed": True}},
                    {},
                    result,
                    request_id="fixture-a1-decode",
                )
                async for _ in stream:
                    pass
                legs["cross_engine" if target.endswith("1") else "same_engine"] = self.calls[-1]
            record[name] = {
                "prefill_call": self.calls[0],
                "descriptor": {
                    "connector": result.connector,
                    "producer_url": result.producer_url,
                    "endpoint": result.endpoint,
                    "producer_request_id": result.producer_request_id,
                    "parameters": result.parameters(),
                },
                "decode_calls": legs,
            }
        assert_golden(self, "requests_prefill_descriptors", record)

    async def test_handoff_bound_from_attested_lease(self):
        record = {}
        for seconds in (6, 7, 30, 60):
            payload = launch("--kv-transfer-config", json.dumps(nixl_config(seconds, ucx=True)))
            self.router.attested("p", payload)
            state = (await self.client.get("/narwhal/state")).json()["handoff"]["p"]
            record[str(seconds)] = {"bound": handoff_bound(self.router, "p"), "state": state}
        assert_golden(self, "requests_handoff_bound", record)
