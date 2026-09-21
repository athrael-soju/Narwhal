"""Check control-pool inference probes and malformed engine responses."""

import asyncio
import json
import unittest
from unittest.mock import patch

import httpx

from narwhal.engines.client import (
    EngineClient,
    EngineError,
    ProbeLeg,
    leg_failure_class,
)
from narwhal.engines.stream import sse_error
from narwhal.types import (
    LEG_CONNECTION,
    LEG_INFERENCE_STATUS,
    LEG_KV_HANDOFF,
    LEG_OVERLOAD,
    LEG_STREAM,
    LEG_TIMEOUT,
)


class EngineProbeTests(unittest.IsolatedAsyncioTestCase):
    """A local transport distinguishes each producer, consumer and control-pool failure."""

    def setUp(self):
        self.responses = {}
        self.calls = []
        self.client = EngineClient(
            model="stub",
            engine_api_key="synthetic-engine-key",
            transport=httpx.MockTransport(self.handle),
            control_connections=0,
        )
        self.addAsyncCleanup(self.client.aclose)

    def handle(self, request):
        """Select a synthetic response by request phase and record the actual dispatch."""
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
        """Health results distinguish local pool pressure from engine failure."""
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

    async def test_tokenizer_failures_preserve_the_unknown_length_result(self):
        """Transport, status and malformed JSON failures leave exact length unavailable."""
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

    async def test_cross_engine_probe_binds_one_fresh_prompt_and_producer_descriptor(self):
        """Both legs share a unique prompt and the consumer receives the fresh handoff."""
        result = await self.client.probe_inference("http://decode", prefill_url="http://prefill")
        self.assertEqual((result.prefill, result.decode), (ProbeLeg(), ProbeLeg()))
        producer, consumer = self.calls
        self.assertEqual(producer[1]["prompt"], consumer[1]["prompt"])
        self.assertEqual(consumer[1]["kv_transfer_params"]["remote_engine_id"], "e0")
        self.assertTrue(
            all(
                headers["authorization"] == "Bearer synthetic-engine-key"
                for _, _, headers in self.calls
            )
        )
        await self.client.probe_inference("http://decode", prefill_url="http://prefill")
        self.assertNotEqual(self.calls[0][1]["prompt"], self.calls[2][1]["prompt"])

    async def test_prefill_probe_failures_leave_cross_engine_consumers_inconclusive(self):
        """The decode probe requires a successful prefill."""
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
        """Consumer probes distinguish pool starvation, overload and incomplete streams."""
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
        """An absolute leg deadline cancels blocked work and returns the phase's failure class."""

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
        """Malformed handoffs surface as phase-specific errors before continuation dispatch."""
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
        """SSE errors default to HTTP 500 when their status code is invalid."""
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
                    sse_error("data: " + json.dumps({"error": payload})), (status, text)
                )
        self.responses["decode"] = httpx.Response(503, text="unavailable")
        with self.assertRaises(EngineError) as caught:
            await anext(self.client.decode("http://engine", "/v1/completions", {}, {}, None))
        self.assertEqual((caught.exception.status, caught.exception.detail), (503, "unavailable"))

    def test_explicit_auth_headers_take_precedence_case_insensitively(self):
        """An explicit authorization header takes precedence over the engine credential."""
        headers = {"Authorization": "Bearer forwarded", "x-request-id": "r"}
        self.assertEqual(self.client._auth(headers), headers)
        self.assertEqual(self.client._auth({})["authorization"], "Bearer synthetic-engine-key")
