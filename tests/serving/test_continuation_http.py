"""Exercise opted-in continuation through HTTP with synthetic qualified engines."""

import asyncio
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from narwhal.config import ContinuationPolicy
from narwhal.profiling.store import ProfileStore
from narwhal.serving.app import create_app
from narwhal.serving.continuation import HistoryReservation
from narwhal.serving.router import NarwhalRouter
from tests.engines.replay_fixtures import envelope, fixture, wire, write_qualification
from tests.fixtures import fleet


class CompletionStream(httpx.AsyncByteStream):
    """Yield raw SSE chunks and optionally keep the response open until cancelled."""

    def __init__(self, chunks, *, hold=False):
        self.chunks = chunks
        self.hold = hold
        self.closed = False
        self.waiting = asyncio.Event()

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.hold:
            self.waiting.set()
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


class ContinuationHttpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.cfg = fleet(self.root)
        self.cfg.tokenize = False
        self.cfg.admission = "open"
        self.cfg.failure_quarantine_s = 0
        self.qualification, _, self.attestation = fixture(
            model=self.cfg.model, engine_ids=tuple(spec.iid for spec in self.cfg.engines)
        )
        path = self.root / "qualification.json"
        pin = write_qualification(path, self.qualification)
        self.cfg.continuation = ContinuationPolicy(
            enabled=True,
            max_attempts=1,
            recovery_budget=1,
            max_context_tokens=64,
            max_history_bytes=16384,
            max_retained_bytes=16384,
            qualification_path=str(path),
            qualification_sha256=pin,
        )
        self.by_host = {httpx.URL(spec.url).host: spec.iid for spec in self.cfg.engines}
        self.calls = []
        self.process_start = 100.0
        self.prefill_status = 200
        self.decode_status = 200
        self.error_body = "SYNTHETIC_PRIVATE_BACKEND_CONTENT"
        self.prefill_hold = None
        self.prefill_started = asyncio.Event()
        self.prefill_clock = []
        self.streams = []
        self.stream_chunks = []
        self.hold_stream = False
        self.restart_on_decode = False
        self.chunks = [
            wire(envelope()),
            wire(envelope((8,), "y", prompt=None, finish="length")),
            b"data: [DONE]\n\n",
        ]

    async def engine(self, request):
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body))
        iid = self.by_host[request.url.host]
        if request.method == "GET":
            if request.url.path == "/version":
                return httpx.Response(200, json={"version": "test-version"})
            if request.url.path == "/metrics":
                return httpx.Response(
                    200, text=f"process_start_time_seconds {self.process_start}\n"
                )
            if request.url.path == "/v1/attestation":
                return httpx.Response(200, json=self.attestation)
            if request.url.path == "/v1/attestation/continuation":
                return httpx.Response(200, json=self.qualification.engines[iid].fields())
            raise AssertionError(request.url.path)
        self.assertEqual(request.url.path, "/v1/completions")
        if body.get("kv_transfer_params", {}).get("do_remote_decode"):
            self.prefill_started.set()
            self.prefill_clock.append(self.router._clock())
            if self.prefill_hold is not None:
                await self.prefill_hold.wait()
            if self.prefill_status != 200:
                return httpx.Response(self.prefill_status, text=self.error_body)
            return httpx.Response(
                200,
                json={"kv_transfer_params": {"remote_engine_id": iid, "remote_block_ids": [0]}},
            )
        if self.decode_status != 200:
            return httpx.Response(self.decode_status, text=self.error_body)
        if self.restart_on_decode:
            self.process_start += 1
        chunks = self.stream_chunks.pop(0) if self.stream_chunks else self.chunks
        stream = CompletionStream(chunks, hold=self.hold_stream)
        self.streams.append(stream)
        return httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})

    def client(self):
        def router(*args, **kwargs):
            return NarwhalRouter(*args, transport=httpx.MockTransport(self.engine), **kwargs)

        with patch("narwhal.serving.app.NarwhalRouter", side_effect=router):
            app = create_app(self.cfg, journal_path=self.root / "journal.jsonl")
        self.router = app.state.router
        self.router.lifecycle.process_starts = dict.fromkeys(self.qualification.engines, 100.0)
        self.router.lifecycle.identities_ready = True
        self.router.journal.open()
        self.addCleanup(self.router.journal.close)
        self.addAsyncCleanup(self.router.engines.aclose)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://router")
        self.addAsyncCleanup(client.aclose)
        return client

    def body(self, **changes):
        return {
            "model": self.cfg.model,
            "prompt": [3],
            "max_tokens": 2,
            "stream": True,
            "narwhal_continuation": True,
            **changes,
        }

    async def post(self, client, **changes):
        return await client.post("/v1/completions", json=self.body(**changes))

    def events(self, response):
        return [
            json.loads(line[5:])
            for line in response.text.splitlines()
            if line.startswith("data:") and line[5:].strip() != "[DONE]"
        ]

    def terminal_rows(self):
        return [
            row
            for line in (self.root / "journal.jsonl").read_text().splitlines()
            if "terminal" in (row := json.loads(line)) and "rid" in row
        ]

    def assert_released(self):
        self.assertEqual(self.router.continuation_memory.used, 0)
        self.assertEqual(self.router.inflight, 0)
        self.assertEqual(self.router.ingress_inflight, 0)
        self.assertFalse(self.router.monitor.waiting)
        for instance in self.router.monitor.instances.values():
            self.assertFalse(instance.prefill)
            self.assertFalse(instance.decode)

    def assert_private_content_absent(self, response):
        self.assertNotIn(self.error_body, response.text)
        self.assertNotIn(self.error_body, (self.root / "journal.jsonl").read_text())

    async def test_success_has_one_original_identity_prompt_usage_and_accounting(self):
        usage = envelope(prompt=None)
        usage["choices"] = []
        usage["usage"] = {"prompt_tokens": 20, "completion_tokens": 30, "total_tokens": 50}
        usage["system_fingerprint"] = "private-backend-fingerprint"
        self.chunks.insert(-1, wire(usage))
        before = int(time.time())
        client = self.client()
        response = await self.post(
            client, return_token_ids=True, stream_options={"include_usage": True}
        )
        self.assertEqual(response.status_code, 200)
        events = self.events(response)
        self.assertEqual(len(events), 3)
        rid = response.headers["x-request-id"]
        self.assertEqual({event["id"] for event in events}, {"cmpl-" + rid})
        self.assertEqual({event["model"] for event in events}, {self.cfg.model})
        self.assertEqual(len({event["created"] for event in events}), 1)
        self.assertGreaterEqual(events[0]["created"], before)
        self.assertLessEqual(events[0]["created"], int(time.time()))
        self.assertNotIn("private-backend-fingerprint", response.text)
        choices = [choice for event in events for choice in event["choices"]]
        self.assertEqual("".join(choice["text"] for choice in choices), "xy")
        self.assertEqual([token for choice in choices for token in choice["token_ids"]], [7, 8])
        self.assertEqual(
            [choice["prompt_token_ids"] for choice in choices if "prompt_token_ids" in choice],
            [[3]],
        )
        self.assertEqual(choices[-1]["finish_reason"], "length")
        self.assertEqual(
            events[-1]["usage"], {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}
        )
        self.assertEqual(response.text.count("data: [DONE]\n\n"), 1)
        row = self.terminal_rows()[0]
        self.assertEqual(row["rid"], rid)
        self.assertEqual(row["terminal"], "completed")
        self.assertEqual(row["output_len"], 2)
        self.assertEqual(row["decode_tokens_observed"], 2)
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["decode_attempts"], 1)
        self.assertEqual(row["attempt_failures"], [])
        self.assertEqual(self.router.offered, 1)
        self.assertEqual(self.router.served, 1)
        self.assertEqual(self.router.decode_tokens_observed, 2)
        posts = [body for method, _, body in self.calls if method == "POST"]
        self.assertEqual(len(posts), 2)
        self.assertTrue(all("narwhal_continuation" not in body for body in posts))
        self.assertEqual(posts[-1]["temperature"], 0)
        self.assertEqual(posts[-1]["stream_interval"], 1)
        self.assertTrue(posts[-1]["return_token_ids"])
        self.assertTrue(all(stream.closed for stream in self.streams))
        self.assert_released()

    async def test_cancelled_asgi_send_closes_engine_before_history_release(self):
        self.client()
        response = await self.router.serve("/v1/completions", self.body(), {})
        sending = asyncio.Event()
        released = []
        original_close = HistoryReservation.close

        def checked_close(reservation):
            self.assertEqual(len(self.streams), 1)
            self.assertTrue(self.streams[0].closed)
            self.assertEqual(self.router.continuation_memory.used, 16384)
            released.append(True)
            original_close(reservation)

        async def receive():
            await asyncio.Event().wait()

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                sending.set()
                await asyncio.Event().wait()

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/v1/completions",
            "headers": [],
            "asgi": {"version": "3.0", "spec_version": "2.4"},
        }
        with patch.object(HistoryReservation, "close", checked_close):
            task = asyncio.create_task(response(scope, receive, send))
            try:
                await asyncio.wait_for(sending.wait(), 2)
                self.assertFalse(self.streams[0].closed)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(released, [True])
        self.assertTrue(self.streams[0].closed)
        self.assert_released()
        row = self.terminal_rows()[0]
        self.assertEqual(row["terminal"], "cancelled")
        self.assertEqual(row["output_len"], 0)
        self.assertEqual(row["decode_tokens_observed"], 1)

    async def test_internal_token_ids_are_hidden_without_explicit_return_ids(self):
        response = await self.post(self.client())
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("token_ids", response.text)
        self.assertEqual(self.terminal_rows()[0]["output_len"], 2)
        self.assert_released()

    async def test_empty_text_ids_are_retained_and_original_prompt_is_exposed_once(self):
        self.chunks = [
            wire(envelope((7,), "")),
            wire(envelope((8,), "🦄", prompt=None)),
            wire(envelope((9,), "!", prompt=None, finish="length")),
            b"data: [DONE]\n\n",
        ]
        response = await self.post(self.client(), max_tokens=3, return_token_ids=True)
        choices = [event["choices"][0] for event in self.events(response)]
        self.assertEqual([choice["text"] for choice in choices], ["", "🦄", "!"])
        self.assertEqual([choice["token_ids"] for choice in choices], [[7], [8], [9]])
        self.assertEqual(
            [choice["prompt_token_ids"] for choice in choices if "prompt_token_ids" in choice],
            [[3]],
        )
        self.assertEqual(self.terminal_rows()[0]["output_len"], 3)
        self.assert_released()

    async def test_preoutput_retry_discards_pending_ids_and_preserves_original_accounting(self):
        self.cfg.serving = replace(
            self.cfg.serving,
            max_attempts=2,
            handoff_timeout_s=5,
            retry_base_s=0.001,
            retry_cap_s=0.001,
        )
        self.stream_chunks = [[wire(envelope((99,), ""))], self.chunks]
        response = await self.post(self.client(), return_token_ids=True)
        self.assertEqual(response.status_code, 200)
        choices = [event["choices"][0] for event in self.events(response)]
        self.assertEqual([choice["token_ids"] for choice in choices], [[7], [8]])
        self.assertEqual(
            [choice["prompt_token_ids"] for choice in choices if "prompt_token_ids" in choice],
            [[3]],
        )
        row = self.terminal_rows()[0]
        self.assertEqual(row["output_len"], 2)
        self.assertEqual(row["decode_tokens_observed"], 3)
        self.assertEqual(row["attempts"], 2)
        self.assertEqual(row["decode_attempts"], 2)
        self.assertTrue(row["attempt_failures"][0]["retry_scheduled"])
        self.assertFalse(row["attempt_failures"][0]["output_started"])
        retried_at = self.prefill_clock[1] - row["arrived"]
        self.assertGreaterEqual(row["ttft_s"], retried_at)
        self.assertGreaterEqual(row["first_byte_s"], retried_at)
        self.assertEqual(self.router.offered, 1)
        self.assertEqual(self.router.served, 1)
        self.assertEqual(self.router.retry_budget.spent, 1)
        self.assertEqual(self.router.continuation_budget.spent, 0)
        self.assertEqual(self.router.continuation_memory.high_water, 16384)
        self.assertTrue(all(stream.closed for stream in self.streams))
        self.assert_released()

    async def test_disabled_deployment_keeps_absent_and_false_requests_ordinary(self):
        self.cfg.continuation = ContinuationPolicy()
        client = self.client()
        for opt_in in (None, False):
            with self.subTest(opt_in=opt_in):
                body = self.body(temperature=0.7, prompt="ordinary text")
                if opt_in is None:
                    body.pop("narwhal_continuation")
                else:
                    body["narwhal_continuation"] = opt_in
                response = await client.post("/v1/completions", json=body)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.events(response)[0]["id"], "request")
                self.assert_released()
        self.assertTrue(all(method == "POST" for method, _, _ in self.calls))
        self.assertTrue(all("narwhal_continuation" not in body for _, _, body in self.calls))
        self.assertEqual(self.router.continuation_memory.high_water, 0)

    async def test_enabled_deployment_requires_explicit_opt_in(self):
        client = self.client()
        response = await self.post(client, narwhal_continuation=False, temperature=0.7)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.events(response)[0]["id"], "request")
        self.assertTrue(all(method == "POST" for method, _, _ in self.calls))
        self.assertEqual(self.router.continuation_memory.high_water, 0)
        self.assert_released()

    async def test_disabled_explicit_opt_in_is_rejected_before_engine_io(self):
        self.cfg.continuation = ContinuationPolicy()
        response = await self.post(self.client())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["message"], "stream continuation is disabled")
        self.assertEqual(self.calls, [])
        self.assert_released()

    async def test_enabled_policy_requires_a_dialect_with_exact_token_ids(self):
        with (
            patch(
                "narwhal.serving.router.lookup_dialect",
                return_value=SimpleNamespace(token_ids=False),
            ),
            self.assertRaisesRegex(ValueError, "dialect with exact token IDs"),
        ):
            self.client()
        self.assertEqual(self.calls, [])

    async def test_unsupported_requests_fail_before_engine_io(self):
        client = self.client()
        variants = [
            {"prompt": self.error_body},
            {"prompt": []},
            {"prompt": [True]},
            {"prompt": [[3]]},
            {"prompt": [256]},
            {"max_tokens": None},
            {"max_tokens": 0},
            {"max_tokens": 33},
            {"stream": False},
            {"n": 2},
            {"temperature": 0.5},
            {"unknown_field": self.error_body},
            {"stop": self.error_body},
            {"stop_token_ids": [4]},
            {"stream_options": {"continuous_usage_stats": True}},
            {"narwhal_continuation": "true"},
        ]
        for changes in variants:
            with self.subTest(changes=changes):
                response = await self.post(client, **changes)
                self.assertEqual(response.status_code, 400)
                self.assert_private_content_absent(response)
                self.assert_released()
        response = await client.post("/v1/chat/completions", json=self.body())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.terminal_rows()), len(variants) + 1)

    async def test_global_history_quota_refuses_second_request_without_engine_io(self):
        self.prefill_hold = asyncio.Event()
        client = self.client()
        first = asyncio.create_task(self.post(client))
        try:
            await asyncio.wait_for(self.prefill_started.wait(), 2)
            self.assertEqual(self.router.continuation_memory.used, 16384)
            calls_before = len(self.calls)
            second = await self.post(client)
            self.assertEqual(second.status_code, 429)
            self.assertEqual(
                second.json()["error"]["message"], "Continuation history capacity is occupied"
            )
            self.assertIn("retry-after", second.headers)
            self.assertEqual(len(self.calls), calls_before)
            self.assertEqual(self.router.continuation_memory.used, 16384)
            self.prefill_hold.set()
            response = await asyncio.wait_for(first, 2)
            self.assertEqual(response.status_code, 200)
        finally:
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
        self.assertEqual(self.router.continuation_memory.high_water, 16384)
        self.assertEqual(self.router.served, 1)
        self.assertEqual(self.router.rejected, 1)
        self.assertEqual(len(self.terminal_rows()), 2)
        self.assert_released()

    async def test_request_that_cannot_fit_reserved_history_fails_before_engine_io(self):
        self.cfg.continuation = replace(
            self.cfg.continuation, max_history_bytes=1, max_retained_bytes=1
        )
        response = await self.post(self.client())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["error"]["message"], "request cannot fit its history quota"
        )
        self.assertEqual(self.calls, [])
        self.assertEqual(self.terminal_rows()[0]["terminal"], "invalid")
        self.assert_released()

    async def test_pending_history_overflow_is_explicit_and_does_not_blame_engine(self):
        self.cfg.continuation = replace(
            self.cfg.continuation, max_history_bytes=8192, max_retained_bytes=8192
        )
        self.chunks = [
            wire(envelope((token,), "", prompt=(3,) if token == 3 else None))
            for token in range(3, 35)
        ]
        client = self.client()
        before = self.router.scheduler.breaker_snapshot()
        response = await self.post(client, max_tokens=32)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.events(response)[0]["error"]["message"],
            "Continuation history or output limit exceeded",
        )
        self.assertNotIn("choices", response.text)
        self.assertNotIn("[DONE]", response.text)
        row = self.terminal_rows()[0]
        self.assertEqual(row["terminal"], "failed")
        self.assertEqual(row["output_len"], 0)
        self.assertGreater(row["decode_tokens_observed"], 1)
        self.assertLess(row["decode_tokens_observed"], 32)
        self.assertEqual(row["attempt_failures"][0]["error_type"], "HistoryLimitExceeded")
        self.assertEqual(self.router.scheduler.breaker_snapshot(), before)
        self.assertEqual(self.router.scheduler.quarantine_list(), [])
        self.assertTrue(self.streams[0].closed)
        self.assert_released()

    async def test_changed_process_is_unavailable_without_dispatch_or_breaker_failure(self):
        client = self.client()
        before = self.router.scheduler.breaker_snapshot()
        self.process_start = 101.0
        response = await self.post(client)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["error"]["message"], "Qualified continuation capacity is unavailable"
        )
        self.assertTrue(self.calls)
        self.assertTrue(all(method == "GET" for method, _, _ in self.calls))
        self.assertEqual(self.router.scheduler.breaker_snapshot(), before)
        self.assertEqual(self.router.scheduler.quarantine_list(), [])
        self.assertEqual(self.terminal_rows()[0]["attempts"], 0)
        self.assert_released()

    async def test_process_change_while_decode_opens_prevents_all_output_commitment(self):
        self.restart_on_decode = True
        client = self.client()
        before = self.router.scheduler.breaker_snapshot()
        response = await self.post(client)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.events(response)[0]["error"]["message"],
            "Qualified continuation capacity is unavailable",
        )
        self.assertNotIn("choices", response.text)
        self.assertNotIn("[DONE]", response.text)
        self.assertEqual(self.router.scheduler.breaker_snapshot(), before)
        self.assertEqual(self.router.scheduler.quarantine_list(), [])
        row = self.terminal_rows()[0]
        self.assertEqual(row["output_len"], 0)
        self.assertEqual(row["decode_tokens_observed"], 0)
        self.assertEqual(row["attempts"], 1)
        self.assertTrue(self.streams[0].closed)
        self.assert_released()

    async def test_wrong_prompt_echo_never_exposes_completion_or_backend_content(self):
        self.chunks = [wire(envelope(text=self.error_body, prompt=(4,), finish="length"))]
        response = await self.post(self.client())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.events(response)[0]["error"]["message"], "Upstream request failed")
        self.assertNotIn("choices", response.text)
        self.assertNotIn("[DONE]", response.text)
        self.assert_private_content_absent(response)
        row = self.terminal_rows()[0]
        self.assertEqual(row["terminal"], "failed")
        self.assertEqual(row["output_len"], 0)
        self.assertIn("invalid continuation stream", row["attempt_failures"][0]["error_message"])
        self.assert_released()

    async def test_decode_rejection_redacts_backend_content_in_response_and_journal(self):
        self.decode_status = 500
        response = await self.post(self.client())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.events(response)[0]["error"]["message"], "Upstream request failed")
        self.assert_private_content_absent(response)
        row = self.terminal_rows()[0]
        self.assertEqual(row["attempt_failures"][0]["status"], 500)
        self.assertEqual(row["output_len"], 0)
        self.assert_released()

    async def test_prefill_rejections_redact_backend_content_in_every_journal_field(self):
        client = self.client()
        for status in (400, 500):
            with self.subTest(status=status):
                self.prefill_status = status
                response = await self.post(client)
                self.assertEqual(response.status_code, 502)
                self.assertEqual(response.json()["error"]["message"], "Upstream request failed")
                self.assert_private_content_absent(response)
                row = self.terminal_rows()[-1]
                self.assertEqual(row["attempt_failures"][0]["status"], status)
                self.assertEqual(row["terminal"], "failed")
                self.assertEqual(row["decode_attempts"], 0)
                self.assert_released()

    async def test_original_deadline_closes_stream_and_releases_history_after_prefix(self):
        self.cfg.request_timeout_s = 0.1
        self.chunks = [wire(envelope())]
        self.hold_stream = True
        response = await self.post(self.client())
        self.assertEqual(response.status_code, 200)
        events = self.events(response)
        self.assertEqual(events[0]["choices"][0]["text"], "x")
        self.assertEqual(events[-1]["error"]["message"], "Request deadline expired")
        self.assertNotIn("[DONE]", response.text)
        self.assertEqual(self.terminal_rows()[0]["terminal"], "expired")
        self.assertEqual(self.terminal_rows()[0]["output_len"], 1)
        self.assertEqual(self.router.expired, 1)
        self.assertTrue(self.streams[0].closed)
        self.assert_released()

    async def test_finish_and_usage_remain_hidden_until_complete_done_delimiter(self):
        usage = envelope(prompt=None)
        usage["choices"] = []
        usage["usage"] = {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}
        self.chunks = [*self.chunks[:-1], wire(usage), b"data: [DONE]\n"]
        profiles = ProfileStore(self.cfg.profiles_path)
        for spec in self.cfg.engines:
            profiles.put(
                replace(
                    profiles.get(spec.iid),
                    generation_digest=self.attestation["attestation_digest"],
                )
            )
        client = self.client()
        self.assertTrue(self.router.continuation_budget.acquire())
        response = await self.post(client, return_token_ids=True)
        self.assertEqual(response.status_code, 200)
        events = self.events(response)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["choices"][0]["text"], "x")
        self.assertIsNone(events[0]["choices"][0]["finish_reason"])
        self.assertEqual(
            events[-1]["error"]["message"], "Continuation recovery credits are exhausted"
        )
        self.assertEqual(events[-1]["error"]["code"], "continuation_shared_budget")
        self.assertNotIn("[DONE]", response.text)
        self.assertNotIn('"usage"', response.text)
        row = self.terminal_rows()[0]
        self.assertEqual(row["terminal"], "failed")
        self.assertEqual(row["output_len"], 1)
        self.assertEqual(row["decode_tokens_observed"], 2)
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["continuation"]["attempts"], 0)
        self.assertEqual(row["continuation"]["terminal_reason"], "shared_budget")
        self.assertTrue(self.streams[0].closed)
        self.assert_released()
