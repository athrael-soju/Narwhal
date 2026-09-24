"""Check HTTP admission, retries, body limits and original-request accounting."""

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx

from narwhal.serving.app import create_app
from narwhal.serving.policy import ServingPolicy
from narwhal.serving.router import NarwhalRouter
from tests.fixtures import fleet, invalid_token_choices


class HttpAccountingTests(unittest.IsolatedAsyncioTestCase):
    """Requests traverse ingress and the real router against a local HTTP transport."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.cfg = fleet(self.root)
        self.cfg.tokenize = False
        self.cfg.admission = "open"
        self.calls = []
        self.prefill_statuses = []
        self.decode_frames = [
            {"choices": [{"index": 0, "text": "x", "token_ids": [1], "finish_reason": "length"}]},
        ]
        self.blocked = None
        self.started = asyncio.Event()

    async def engine(self, request):
        """Record upstream headers and serve a producer descriptor or decode stream."""
        body = json.loads(request.content)
        self.calls.append((dict(request.headers), body))
        if (body.get("kv_transfer_params") or {}).get("do_remote_decode"):
            self.started.set()
            if self.blocked is not None:
                await self.blocked.wait()
            status = self.prefill_statuses.pop(0) if self.prefill_statuses else 200
            return httpx.Response(
                status,
                json={"kv_transfer_params": {"remote_engine_id": "e0", "remote_block_ids": [0]}},
            )
        wire = "".join("data: " + json.dumps(frame) + "\n\n" for frame in self.decode_frames)
        return httpx.Response(200, text=wire + "data: [DONE]\n\n")

    def client(self):
        """Create the app and register cleanup for its journal, engine client and HTTP client."""

        def router(*args, **kwargs):
            return NarwhalRouter(*args, transport=httpx.MockTransport(self.engine), **kwargs)

        with patch("narwhal.serving.app.NarwhalRouter", side_effect=router):
            app = create_app(self.cfg, journal_path=self.root / "journal.jsonl")
        self.router = app.state.router
        self.router.lifecycle.process_starts = {spec.iid: 100 for spec in self.cfg.engines}
        self.router.lifecycle.identities_ready = True
        self.router.journal.open()
        self.addCleanup(self.router.journal.close)
        self.addAsyncCleanup(self.router.engines.aclose)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router")
        self.addAsyncCleanup(client.aclose)
        return client

    async def post(self, client, **kwargs):
        """Submit one ordinary completion through HTTP."""
        return await client.post(
            "/v1/completions",
            json={"model": self.cfg.model, "prompt": "hello", "max_tokens": 1, "stream": False},
            **kwargs,
        )

    def terminal_rows(self):
        """Read original request rows from the flushed journal."""
        return [
            row
            for line in (self.root / "journal.jsonl").read_text().splitlines()
            if "meta" not in (row := json.loads(line))
        ]

    def assert_released(self):
        """Every original request releases ingress, active and scheduler reservations."""
        self.assertEqual(self.router.inflight, 0)
        self.assertEqual(self.router.ingress_inflight, 0)
        self.assertFalse(self.router.monitor.waiting)

    async def test_readiness_blocks_traffic_until_identity_capture(self):
        """Declared engines require captured process identities before admission."""
        client = self.client()
        self.router.lifecycle.identities_ready = False
        response = await self.post(client)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.calls, [])
        self.assert_released()

    async def test_client_credentials_stop_at_ingress_and_correlation_is_preserved(self):
        """Engine legs receive router correlation IDs and deployment-owned credentials."""
        client = self.client()
        response = await self.post(
            client,
            headers={
                "authorization": "Bearer synthetic-key",
                "x-api-key": "synthetic-key",
                "x-request-id": "client-rid",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.calls)
        for headers, _ in self.calls:
            self.assertNotIn("authorization", headers)
            self.assertNotIn("x-api-key", headers)
            self.assertIn("x-request-id", headers)
        self.assert_released()

    async def test_retry_repeats_prefill_and_settles_one_original(self):
        """A transient producer failure consumes one retry and records one served request."""
        self.cfg.serving = ServingPolicy(
            max_attempts=2, handoff_timeout_s=5, retry_base_s=0.001, retry_cap_s=0.001
        )
        self.cfg.failure_quarantine_s = 0
        self.prefill_statuses = [503, 200]
        client = self.client()
        response = await self.post(client)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            sum(
                bool(body.get("kv_transfer_params", {}).get("do_remote_decode"))
                for _, body in self.calls
            ),
            2,
        )
        self.assertEqual(self.router.served, 1)
        self.assertEqual(self.router.failed, 0)
        self.assertEqual(len(self.terminal_rows()), 1)
        self.assert_released()

    async def test_invalid_and_oversized_bodies_settle_before_dispatch(self):
        """Malformed JSON and streamed body limits each produce one terminal record."""
        self.cfg.serving = replace(self.cfg.serving, max_request_bytes=64)
        client = self.client()
        bad = await client.post(
            "/v1/completions", content="{", headers={"content-type": "application/json"}
        )
        self.assertEqual(bad.status_code, 400)
        large = await client.post("/v1/completions", content="x" * 65)
        self.assertEqual(large.status_code, 413)
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.terminal_rows()), 2)
        self.assert_released()

    async def test_client_cancellation_releases_prefill_ownership(self):
        """Cancelling a client during prefill records cancellation and releases all seats."""
        self.blocked = asyncio.Event()
        client = self.client()
        task = asyncio.create_task(self.post(client))
        await asyncio.wait_for(self.started.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_released()
        self.assertEqual(self.router.cancelled, 1)
        self.assertEqual(self.router.served, 0)
        self.assertEqual(len(self.terminal_rows()), 1)

    async def test_original_deadline_cancels_blocked_prefill(self):
        """The original request deadline expires while the producer remains blocked."""
        self.cfg.request_timeout_s = 0.02
        self.blocked = asyncio.Event()
        client = self.client()
        response = await self.post(client)
        self.assertEqual(response.status_code, 504)
        self.assert_released()
        self.assertEqual(len(self.terminal_rows()), 1)

    async def test_predictive_refusals_distinguish_prompt_queue_and_unpriced_work(self):
        """Temporary refusals include a cause and Retry-After."""
        self.cfg.admission = "predictive"
        client = self.client()
        for priced, floor, cause, retry in (
            (20, 20, "prompt", False),
            (20, 1, "queue", True),
            (float("inf"), 1, "aggregate_unpriced", True),
        ):
            with (
                self.subTest(cause=cause),
                patch.object(self.router.scheduler, "prefill_admission_price", return_value=priced),
                patch.object(self.router.scheduler, "cheapest_own_prefill", return_value=floor),
            ):
                response = await self.post(client)
                self.assertEqual(response.status_code, 429)
                self.assertEqual("retry-after" in response.headers, retry)
                self.assertEqual(self.terminal_rows()[-1]["refused_cause"], cause)
                self.assert_released()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.router.refused, 3)
        self.assertEqual(self.router.offered, 3)
        self.assertEqual(len(self.terminal_rows()), 3)
        self.assertEqual(len({row["rid"] for row in self.terminal_rows()}), 3)
        self.assertFalse(self.router._seat_since)
        for instance in self.router.monitor.instances.values():
            self.assertFalse(instance.prefill)
            self.assertFalse(instance.decode)

    async def test_invalid_output_identity_fails_the_original_request(self):
        """Serving rejects unidentified output before committing a successful response."""
        client = self.client()
        for choice in invalid_token_choices():
            with self.subTest(choice=choice):
                self.decode_frames = [{"choices": [choice]}]
                response = await self.post(client)
                self.assertEqual(response.status_code, 502)
                row = self.terminal_rows()[-1]
                self.assertEqual(row["terminal"], "failed")
                self.assertIn("token_ids", row["error"])
                self.assert_released()

    async def test_exhausted_retry_budget_settles_the_first_attempt(self):
        """A zero-credit router records the producer failure after one attempt."""
        self.cfg.serving = ServingPolicy(max_attempts=2, handoff_timeout_s=5, retry_budget=0)
        self.prefill_statuses = [503]
        client = self.client()
        response = await self.post(client)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.router.failed, 1)
        self.assertEqual(len(self.terminal_rows()), 1)
        self.assert_released()

    async def test_upstream_trace_stays_in_journal(self):
        trace = "Traceback (most recent call last): /private/engine.py: token=secret-value"
        self.decode_frames.append({"error": {"message": trace}})
        client = self.client()
        response = await self.post(client)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["message"], "Upstream request failed")
        self.assertNotIn(trace, response.text)
        self.assertIn(trace, self.terminal_rows()[-1]["error"])
        self.assert_released()

    async def test_output_started_prevents_retry_after_decode_failure(self):
        """A decode error after the first token ends the stream and records one failed request."""
        self.cfg.serving = ServingPolicy(max_attempts=3, handoff_timeout_s=5)
        self.decode_frames.append({"error": {"message": "decode failed"}})
        client = self.client()
        response = await client.post(
            "/v1/completions",
            json={"model": self.cfg.model, "prompt": "hello", "max_tokens": 1, "stream": True},
        )
        self.assertEqual(response.status_code, 200)
        first = json.loads(response.text.splitlines()[0].removeprefix("data: "))
        self.assertEqual(first["choices"][0]["text"], "x")
        self.assertIn("Upstream request failed", response.text)
        self.assertNotIn("decode failed", response.text)
        self.assertIn("decode failed", self.terminal_rows()[-1]["error"])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.router.failed, 1)
        self.assertEqual(len(self.terminal_rows()), 1)
        self.assert_released()
