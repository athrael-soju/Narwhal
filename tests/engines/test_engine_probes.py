import asyncio
import json
import time
import unittest
from unittest.mock import patch

import httpx

from narwhal.engines.client import (
    LATE_TIMEOUT_FACTOR,
    EngineClient,
    EngineError,
    ProbeLeg,
    leg_failure_class,
)
from narwhal.engines.stream import parse_event, sse_error
from narwhal.types import (
    LEG_CONNECTION,
    LEG_INFERENCE_STATUS,
    LEG_KV_HANDOFF,
    LEG_OVERLOAD,
    LEG_STREAM,
    LEG_TIMEOUT,
)
from tests.serving.test_rendezvous import FakeRendezvous
from tests.wire import engine_transports, vllm_engine


class EngineProbeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.responses = {}
        self.calls = []
        self.client = EngineClient(
            **vllm_engine(),
            model="stub",
            engine_api_key="synthetic-engine-key",
            **engine_transports(self.handle),
            control_connections=0,
        )
        self.addAsyncCleanup(self.client.aclose)

    def handle(self, request):
        body = json.loads(request.content) if request.content else {}
        kind = (
            "health"
            if request.url.path == "/health"
            else "tokenize"
            if request.url.path == "/tokenize"
            else "prefill"
            if body.get("kv_transfer_params", {}).get("do_remote_decode")
            else "decode"
        )
        self.calls.append((kind, body, dict(request.headers)))
        default = {
            "health": httpx.Response(200),
            "tokenize": httpx.Response(200, json={"count": 12}),
            "prefill": httpx.Response(
                200,
                json={"kv_transfer_params": {"remote_engine_id": "e0", "remote_block_ids": [0]}},
            ),
            "decode": httpx.Response(
                200, text='data: {"choices":[{"text":"x","token_ids":[1]}]}\n\ndata: [DONE]\n\n'
            ),
        }
        result = self.responses.get(kind, default[kind])
        if isinstance(result, Exception):
            raise result
        return result

    async def test_control_health_distinguishes_pool_exhaustion_from_engine_failure(self):
        for response, expected in (
            (httpx.PoolTimeout("pool"), None),
            (httpx.ConnectError("engine"), False),
            (httpx.Response(503), False),
            (httpx.Response(200), True),
        ):
            self.responses["health"] = response
            with self.subTest(expected=expected):
                self.assertIs(await self.client.healthy("http://engine"), expected)
        self.assertEqual(self.client.control_connections, 2)

    async def test_a_health_timeout_that_surfaces_late_is_inconclusive(self):
        delay = {"s": 0.0}

        def handle(request):
            time.sleep(delay["s"])
            raise httpx.ReadTimeout("health")

        client = EngineClient(
            **vllm_engine(), transport=httpx.MockTransport(handle), health_timeout_s=0.05
        )
        self.addAsyncCleanup(client.aclose)
        for late_s, expected in ((0.0, False), (0.05 * LATE_TIMEOUT_FACTOR + 0.05, None)):
            delay["s"] = late_s
            with self.subTest(late_s=late_s):
                self.assertIs(await client.healthy("http://engine"), expected)

    async def test_tokenizer_failures_preserve_the_unknown_length_result(self):
        self.assertEqual(
            await self.client.token_count("http://engine", {"model": "stub", "prompt": "x"}, 1), 12
        )
        for response in (
            httpx.ReadTimeout("tokenizer"),
            httpx.Response(503),
            httpx.Response(200, text="{"),
        ):
            self.responses["tokenize"] = response
            self.assertIsNone(await self.client.token_count("http://engine", {"prompt": "x"}, 1))
        with patch.object(self.client.dialect, "tokenize_path", None):
            before = len(self.calls)
            self.assertIsNone(await self.client.token_count("http://engine", {}, 1))
            self.assertEqual(len(self.calls), before)

    async def test_tokenization_retains_prompt_ids_and_forwards_special_token_setting(self):
        self.responses["tokenize"] = httpx.Response(
            200, json={"count": 3, "max_model_len": 64, "tokens": [1, 7, 9]}
        )
        body = {"model": "stub", "prompt": "x", "add_special_tokens": False}
        result = await self.client.tokenize("http://engine", body, 1)
        self.assertEqual((result.count, result.token_ids), (3, (1, 7, 9)))
        sent = self.calls[-1][1]
        self.assertEqual(sent, {"model": "stub", "prompt": "x", "add_special_tokens": False})
        self.assertEqual(await self.client.token_count("http://engine", body, 1), 3)
        for tokens in ([1, 7], [1, -7, 9], None):
            self.responses["tokenize"] = httpx.Response(200, json={"count": 3, "tokens": tokens})
            result = await self.client.tokenize("http://engine", {"prompt": "x"}, 1)
            self.assertEqual((result.count, result.token_ids), (3, None))
            self.assertNotIn("add_special_tokens", self.calls[-1][1])

    async def test_phase_calls_preserve_connection_and_pool_limits(self):
        seen = {}

        def handle(request):
            if request.url.path == "/health":
                seen["/health"] = request.extensions["timeout"]
                return httpx.Response(200)
            if request.url.path == "/tokenize":
                return httpx.Response(200, json={"count": 2})
            return httpx.Response(
                200,
                json={"kv_transfer_params": {"remote_engine_id": "e0", "remote_block_ids": [0]}},
            )

        client = EngineClient(
            **vllm_engine(),
            **engine_transports(handle),
            connect_timeout_s=0.2,
            pool_timeout_s=0.1,
            health_timeout_s=0.5,
            prefill_timeout_s=0.4,
        )
        self.addAsyncCleanup(client.aclose)
        self.assertTrue(await client.healthy("http://engine"))
        self.assertEqual(await client.token_count("http://engine", {"prompt": "x"}, 0.3), 2)
        await client.prefill("http://engine", "/v1/completions", {"prompt": "x"}, {})
        self.assertEqual((seen["/health"]["connect"], seen["/health"]["pool"]), (0.2, 0.1))
        self.assertEqual((client._wire.connect_timeout_s, client._wire.pool_timeout_s), (0.2, 0.1))

    async def test_strict_tokenizer_fails_before_fallback(self):
        async def slow(request):
            await asyncio.sleep(0.3)
            return httpx.Response(200, json={"count": 12})

        client = EngineClient(**vllm_engine(), **engine_transports(slow))
        self.addAsyncCleanup(client.aclose)
        with self.assertRaisesRegex(EngineError, "exact count exceeded 0.05s"):
            await client.token_count("http://engine", {"prompt": "x"}, 0.05, strict=True)

    async def test_prefill_uses_one_elapsed_budget(self):
        async def slow(request):
            await asyncio.sleep(0.08)
            return httpx.Response(200, json={})

        client = EngineClient(**vllm_engine(), **engine_transports(slow), prefill_timeout_s=0.02)
        self.addAsyncCleanup(client.aclose)
        with self.assertRaisesRegex(httpx.ReadTimeout, "prefill exceeded its 0.02s"):
            await client.prefill("http://engine", "/v1/completions", {"prompt": "x"}, {})

    async def test_cross_engine_probe_binds_one_fresh_prompt_and_producer_descriptor(self):
        result = await self.client.probe_inference("http://decode", prefill_url="http://prefill")
        self.assertEqual((result.prefill, result.decode), (ProbeLeg(), ProbeLeg()))
        producer, consumer = self.calls
        self.assertEqual(producer[1]["prompt"], consumer[1]["prompt"])
        self.assertEqual(consumer[1]["kv_transfer_params"]["remote_engine_id"], "e0")
        # A model that ends the probe prompt at once must still emit the probed token.
        self.assertEqual((consumer[1]["min_tokens"], consumer[1]["ignore_eos"]), (1, True))
        self.assertTrue(
            all(
                headers["authorization"] == "Bearer synthetic-engine-key"
                for _, _, headers in self.calls
            )
        )
        await self.client.probe_inference("http://decode", prefill_url="http://prefill")
        self.assertNotEqual(self.calls[0][1]["prompt"], self.calls[2][1]["prompt"])

    async def test_prefill_probe_failures_leave_cross_engine_consumers_inconclusive(self):
        for response, expected in (
            (httpx.PoolTimeout("pool"), ProbeLeg(inconclusive=True)),
            (httpx.ConnectError("connection"), ProbeLeg(failed=LEG_CONNECTION)),
            (httpx.ReadTimeout("timeout"), ProbeLeg(failed=LEG_TIMEOUT)),
            (httpx.Response(429), ProbeLeg(failed=LEG_OVERLOAD)),
            (httpx.Response(503), ProbeLeg(failed=LEG_INFERENCE_STATUS)),
            (httpx.Response(200, json={}), ProbeLeg(failed=LEG_KV_HANDOFF)),
            (httpx.Response(200, text="{"), ProbeLeg(failed=LEG_KV_HANDOFF)),
        ):
            self.calls.clear()
            self.responses["prefill"] = response
            with self.subTest(expected=expected):
                result = await self.client.probe_inference(
                    "http://decode", prefill_url="http://prefill"
                )
                self.assertEqual(result.prefill, expected)
                self.assertEqual(result.decode, ProbeLeg(inconclusive=True))
                self.assertEqual([kind for kind, _, _ in self.calls], ["prefill"])

    async def test_decode_probe_requires_output_followed_by_a_terminator(self):
        for response, expected in (
            (httpx.PoolTimeout("pool"), ProbeLeg(inconclusive=True)),
            (httpx.ConnectError("connection"), ProbeLeg(failed=LEG_CONNECTION)),
            (httpx.Response(408), ProbeLeg(failed=LEG_OVERLOAD)),
            (httpx.Response(500), ProbeLeg(failed=LEG_INFERENCE_STATUS)),
            (httpx.Response(200, text="data: [DONE]\n\n"), ProbeLeg(failed=LEG_STREAM)),
            (
                httpx.Response(200, text='data: {"choices":[{"text":"x"}]}\n\n'),
                ProbeLeg(failed=LEG_STREAM),
            ),
        ):
            self.responses["decode"] = response
            with self.subTest(expected=expected):
                result = await self.client.probe_inference("http://engine")
                self.assertEqual(result.decode, expected)

    async def test_probe_leg_deadlines_cancel_blocked_work(self):

        async def blocked(*args, **kwargs):
            await asyncio.Event().wait()

        for method, expected in (("_probe_prefill", LEG_TIMEOUT), ("_probe_decode", LEG_STREAM)):
            with (
                self.subTest(method=method),
                patch.object(self.client, method, side_effect=blocked),
            ):
                result = await self.client.probe_inference("http://engine", deadline_s=0.001)
                self.assertEqual(
                    getattr(result, "prefill" if method == "_probe_prefill" else "decode").failed,
                    expected,
                )
        self.client.model = ""
        self.assertIsNone(await self.client.probe_inference("http://engine"))

    async def test_invalid_prefill_payload_and_decode_continuation_raise_typed_errors(self):
        self.responses["prefill"] = httpx.Response(200, json={})
        with self.assertRaisesRegex(EngineError, "no handoff") as caught:
            await self.client.prefill("http://engine", "/v1/completions", {}, {})
        self.assertEqual(leg_failure_class(caught.exception), LEG_KV_HANDOFF)
        with (
            patch.object(self.client.kv, "decode_body", side_effect=ValueError("bad descriptor")),
            self.assertRaisesRegex(EngineError, "invalid continuation"),
        ):
            await anext(self.client.decode("http://engine", "/v1/completions", {}, {}, None))

    async def test_error_stream_shapes_preserve_status_and_message(self):
        for payload, status, text in (
            ({"code": 429, "message": " busy "}, 429, "busy"),
            ({"code": True, "message": "failed"}, 500, "failed"),
            ({"code": 300}, 500, '{"code":300}'),
            ({}, 500, "engine returned an SSE error"),
            (" failed ", 500, "failed"),
            (False, 500, "false"),
        ):
            with self.subTest(payload=payload):
                self.assertEqual(
                    sse_error(parse_event("data: " + json.dumps({"error": payload}))),
                    (status, text),
                )
        self.responses["decode"] = httpx.Response(503, text="unavailable")
        with self.assertRaises(EngineError) as caught:
            await anext(self.client.decode("http://engine", "/v1/completions", {}, {}, None))
        self.assertEqual((caught.exception.status, caught.exception.detail), (503, "unavailable"))

    def test_the_prefill_leg_generates_one_token(self):
        leg = self.client._prefill_leg(
            {"model": "stub", "messages": [], "max_tokens": 64, "max_completion_tokens": 2048}
        )
        self.assertEqual(leg["max_tokens"], 1)
        self.assertNotIn("max_completion_tokens", leg)

    def test_explicit_auth_headers_take_precedence_case_insensitively(self):
        headers = {"Authorization": "Bearer forwarded", "x-request-id": "r"}
        self.assertEqual(self.client._auth(headers), headers)
        self.assertEqual(self.client._auth({})["authorization"], "Bearer synthetic-engine-key")


class RendezvousProbeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls = []
        self.prefill_status = 200
        self.prefill_seen = asyncio.Event()
        self.decode_seen = asyncio.Event()
        self.client = EngineClient(
            kv=FakeRendezvous(),
            dialect=vllm_engine()["dialect"],
            model="stub",
            **engine_transports(self.handle),
        )
        self.addAsyncCleanup(self.client.aclose)

    async def handle(self, request):
        body = json.loads(request.content)
        self.calls.append((str(request.url), body))
        if body["leg"] == "prefill":
            self.prefill_seen.set()
            # Each leg waits for the other, so only concurrent legs complete.
            await asyncio.wait_for(self.decode_seen.wait(), 5)
            return httpx.Response(self.prefill_status, json={})
        self.decode_seen.set()
        if self.prefill_status != 200:
            await asyncio.Event().wait()
        await asyncio.wait_for(self.prefill_seen.wait(), 5)
        return httpx.Response(
            200, text='data: {"choices":[{"text":"x","token_ids":[1]}]}\n\ndata: [DONE]\n\n'
        )

    async def test_directed_probe_runs_both_legs_concurrently_with_one_rendezvous(self):
        result = await self.client.probe_inference(
            "http://decode", prefill_url="http://prefill", deadline_s=2
        )
        self.assertEqual((result.prefill, result.decode), (ProbeLeg(), ProbeLeg()))
        legs = {body["leg"]: (url, body) for url, body in self.calls}
        self.assertTrue(legs["prefill"][0].startswith("http://prefill/"))
        self.assertTrue(legs["decode"][0].startswith("http://decode/"))
        self.assertEqual(legs["prefill"][1]["room"], legs["decode"][1]["room"])
        self.assertEqual(legs["prefill"][1]["prompt"], legs["decode"][1]["prompt"])
        self.assertEqual(legs["prefill"][1]["max_tokens"], 1)

    async def test_failed_prefill_leaves_decode_inconclusive_within_the_wait_bound(self):
        self.prefill_status = 500
        began = time.monotonic()
        result = await self.client.probe_inference(
            "http://decode", prefill_url="http://prefill", deadline_s=5
        )
        self.assertLess(time.monotonic() - began, 2)
        self.assertEqual(result.prefill, ProbeLeg(failed=LEG_INFERENCE_STATUS))
        self.assertEqual(result.decode, ProbeLeg(inconclusive=True))
