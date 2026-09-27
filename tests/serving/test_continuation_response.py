"""Check continuation commitment through the real ASGI response writer."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import httpx
from starlette.requests import ClientDisconnect

from narwhal.engines.replay import ReplayEvent
from narwhal.observability.journal import RunJournal
from narwhal.serving.continuation import ContinuationHistory, HistoryBudget
from narwhal.serving.continuation_output import encode_event
from narwhal.serving.execution import serve_request
from narwhal.serving.lifecycle import RequestLifecycle
from narwhal.serving.response import RequestStreamResponse
from narwhal.serving.router import NarwhalRouter
from tests.fixtures import fleet


def completion(ids, text, finish=None):
    return ReplayEvent(
        kind="completion",
        envelope={},
        generated_ids=tuple(ids),
        text=text,
        finish_reason=finish,
    )


def serialise(event):
    if event.kind == "done":
        return b"data: [DONE]\n\n"
    payload = {
        "choices": [
            {
                "index": 0,
                "text": event.text,
                "token_ids": list(event.generated_ids),
                "finish_reason": event.finish_reason,
            }
        ]
    }
    return ("data: " + json.dumps(payload) + "\n\n").encode()


class ContinuationResponseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.cfg = fleet(self.root)
        journal = RunJournal(self.root / "journal.jsonl")
        journal.open()
        self.addCleanup(journal.close)

        def no_engine_requests(request):
            raise AssertionError("ASGI commitment tests must not dispatch inference")

        self.router = NarwhalRouter(
            self.cfg, journal, transport=httpx.MockTransport(no_engine_requests)
        )
        self.addAsyncCleanup(self.router.engines.aclose)
        self.state = RequestLifecycle.offered(self.router, {"x-request-id": "caller-id"})
        self.state.request.input_len = 2
        self.state.request.wanted_len = 8
        self.state.sized = True
        self.state.phase = "decode"
        self.state.continuation_requested = True
        self.state.admit()
        self.quota = 16384
        self.budget = HistoryBudget(self.quota)
        self.history = ContinuationHistory.create([101, 102], 8, self.budget.reserve(self.quota))
        self.state.continuation = self.history
        self.addCleanup(self.history.close)
        self.stream_closed = asyncio.Event()
        self.events = [
            completion([11], "x"),
            completion([12], "y", "length"),
            ReplayEvent(kind="done", envelope={}),
        ]

    def response(self, upstream=None):
        async def stream():
            try:
                for event in self.events:
                    group = self.history.append(event, serialise(event), self.router._clock())
                    if group is not None:
                        yield group
                        del group
                self.state.finish("completed")
            finally:
                self.stream_closed.set()

        response = RequestStreamResponse(stream() if upstream is None else upstream, self.state)
        self.addAsyncCleanup(response.aclose)
        return response

    def start(self, response, send, *, receive=None, spec="2.4"):
        async def never_disconnect():
            await asyncio.Event().wait()

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/v1/completions",
            "headers": [],
            "asgi": {"version": "3.0", "spec_version": spec},
        }
        task = asyncio.create_task(response(scope, receive or never_disconnect, send))

        async def cleanup():
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.addAsyncCleanup(cleanup)
        return task

    def terminal_rows(self):
        return [
            row
            for line in (self.root / "journal.jsonl").read_text().splitlines()
            if "terminal" in (row := json.loads(line)) and row.get("rid") == self.state.rid
        ]

    def assert_released(self, terminal):
        self.assertTrue(self.history.closed)
        self.assertEqual(self.budget.used, 0)
        self.assertEqual(self.router.inflight, 0)
        rows = self.terminal_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["terminal"], terminal)

    async def test_attempt_transition_retains_original_identity_timing_and_committed_prefix(self):
        self.state.begin_attempt()
        self.state.continuation_created = 123
        self.state.prefilled_at = self.state.arrived + 0.1
        self.state.first_at = self.state.arrived + 0.2
        self.state.last_at = self.state.arrived + 0.3
        group = self.history.append(self.events[0], serialise(self.events[0]), 1.0)
        self.history.commit(group, 2.0)
        self.state.tokens = self.history.committed_count
        self.state.output_started = True
        unfinished = completion([12], "")
        self.history.append(unfinished, serialise(unfinished), 3.0)
        original = (
            self.state.rid,
            self.state.client_rid,
            self.state.arrived,
            self.state.deadline,
            self.state.prefilled_at,
            self.state.first_at,
            self.state.last_at,
            self.state.continuation_created,
        )
        self.state.begin_attempt()
        self.assertEqual(
            (
                self.state.rid,
                self.state.client_rid,
                self.state.arrived,
                self.state.deadline,
                self.state.prefilled_at,
                self.state.first_at,
                self.state.last_at,
                self.state.continuation_created,
            ),
            original,
        )
        self.assertEqual(self.history.replay_prompt(), [101, 102, 11])
        self.assertEqual((self.history.max_tokens, self.history.remaining), (8, 7))
        self.assertEqual((self.state.tokens, self.state.request.output_len), (1, 1))
        event = ReplayEvent(
            kind="completion",
            envelope={"choices": [{}]},
            generated_ids=(13,),
            text="z",
        )
        outward = json.loads(encode_event(self.state, event, expose_ids=True)[6:])
        self.assertEqual(outward["id"], "cmpl-" + self.state.rid)
        self.assertEqual(outward["created"], 123)
        self.assertNotIn("prompt_token_ids", outward["choices"][0])
        self.assertFalse(await self.state.retry(httpx.ReadError("transport failed")))
        self.assertEqual(self.state.attempt_failures[-1]["retry_reason"], "output_started")
        self.state.finish("failed", error="synthetic transition test", status=502)
        self.assert_released("failed")

    async def test_direct_entry_validates_opt_in_before_interpreting_generation_fields(self):
        response = await serve_request(
            self.router,
            "direct-invalid",
            "/v1/completions",
            {"narwhal_continuation": True, "max_tokens": {}, "n": {}},
            {},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"stream continuation is disabled", response.body)
        self.assertEqual(self.budget.used, self.quota)
        self.state.finish("cancelled")

    async def test_blocked_send_retains_quota_and_commits_only_after_acceptance(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        bodies = []
        frontiers_at_send = []

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                frontiers_at_send.append(self.history.committed_count)
                if not bodies:
                    entered.set()
                    await release.wait()
                bodies.append(message["body"])

        response = self.response()
        task = self.start(response, send)
        await asyncio.wait_for(entered.wait(), 2)
        self.assertEqual((self.history.observed_count, self.history.committed_count), (1, 0))
        self.assertEqual((self.state.tokens, self.state.output_started), (0, False))
        self.assertEqual(self.budget.used, self.quota)
        self.assertIsNone(self.budget.reserve(1))
        self.assertFalse(self.history.closed)
        release.set()
        await asyncio.wait_for(task, 2)
        self.assertEqual(frontiers_at_send, [0, 1])
        self.assertEqual((self.state.tokens, self.state.output_started), (2, True))
        self.assertTrue(self.history.terminal_committed)
        self.assertEqual(len(bodies), 2)
        self.assertTrue(bodies[-1].endswith(b"data: [DONE]\n\n"))
        self.assertTrue(self.stream_closed.is_set())
        self.assert_released("completed")
        await response.aclose()
        await response.aclose()
        self.assertEqual(self.router.served, 1)
        self.assertEqual(len(self.terminal_rows()), 1)

    async def test_send_failure_never_commits_or_releases_before_writer_unwinds(self):
        held_during_unwind = []

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                try:
                    raise OSError("client write failed")
                finally:
                    held_during_unwind.append(self.budget.used)

        task = self.start(self.response(), send)
        with self.assertRaises(ClientDisconnect):
            await task
        self.assertEqual(held_during_unwind, [self.quota])
        self.assertEqual((self.history.committed_count, self.state.tokens), (0, 0))
        self.assertFalse(self.state.output_started)
        self.assertTrue(self.stream_closed.is_set())
        self.assert_released("cancelled")

    async def test_cancelled_send_keeps_quota_until_writer_finally_runs(self):
        entered = asyncio.Event()
        held_during_unwind = []

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    held_during_unwind.append(self.budget.used)

        task = self.start(self.response(), send)
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(held_during_unwind, [self.quota])
        self.assertEqual((self.history.committed_count, self.state.tokens), (0, 0))
        self.assertFalse(self.state.output_started)
        self.assert_released("cancelled")

    async def test_deadline_during_send_does_not_commit_pending_group(self):
        self.state.deadline = self.router._clock() + 0.1
        held_during_unwind = []
        errors = []

        async def send(message):
            if message["type"] != "http.response.body":
                return
            if message.get("more_body", False):
                try:
                    await asyncio.Event().wait()
                finally:
                    held_during_unwind.append(self.budget.used)
            elif message.get("body"):
                errors.append(message["body"])

        await asyncio.wait_for(self.start(self.response(), send), 2)
        self.assertEqual(held_during_unwind, [self.quota])
        self.assertEqual((self.history.committed_count, self.state.tokens), (0, 0))
        self.assertFalse(self.state.output_started)
        self.assertEqual(len(errors), 1)
        self.assertIn(b'"code": "expired"', errors[0])
        self.assert_released("expired")

    async def test_terminal_accounting_cannot_reuse_quota_while_writer_is_blocked(self):
        entered = asyncio.Event()

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                entered.set()
                await asyncio.Event().wait()

        task = self.start(self.response(), send)
        await asyncio.wait_for(entered.wait(), 2)
        self.state.finish("failed", error="upstream failure", status=502)
        self.assertEqual(self.budget.used, self.quota)
        self.assertFalse(self.history.closed)
        self.assertIsNone(self.budget.reserve(self.quota))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_released("failed")

    async def test_finish_and_empty_text_tokens_wait_for_done_in_one_body(self):
        reached_finish = asyncio.Event()
        allow_done = asyncio.Event()
        bodies = []
        empty = completion([11], "")
        finish = completion([12], "🦄", "length")
        done = ReplayEvent(kind="done", envelope={})

        async def stream():
            self.assertIsNone(self.history.append(empty, serialise(empty), self.router._clock()))
            self.assertIsNone(self.history.append(finish, serialise(finish), self.router._clock()))
            reached_finish.set()
            await allow_done.wait()
            yield self.history.append(done, serialise(done), self.router._clock())
            self.state.finish("completed")

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                bodies.append(message["body"])

        task = self.start(self.response(stream()), send)
        await asyncio.wait_for(reached_finish.wait(), 2)
        self.assertEqual(bodies, [])
        self.assertEqual((self.history.observed_count, self.history.committed_count), (2, 0))
        self.assertFalse(self.state.output_started)
        allow_done.set()
        await asyncio.wait_for(task, 2)
        self.assertEqual(bodies, [serialise(empty) + serialise(finish) + serialise(done)])
        self.assertEqual((self.state.tokens, self.history.committed_count), (2, 2))
        self.assert_released("completed")

    async def test_upstream_failure_before_done_does_not_emit_finish_metadata(self):
        bodies = []

        async def stream():
            finish = completion([11], "x", "stop")
            group = self.history.append(finish, serialise(finish), self.router._clock())
            if group is not None:
                yield group
            self.state.finish("failed", error="upstream stream ended", status=502)
            raise RuntimeError("upstream stream ended")

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                bodies.append(message["body"])

        with self.assertRaisesRegex(RuntimeError, "upstream stream ended"):
            await self.start(self.response(stream()), send)
        self.assertEqual(bodies, [])
        self.assertEqual((self.history.committed_count, self.state.tokens), (0, 0))
        self.assert_released("failed")

    async def test_direct_iterator_yield_does_not_acknowledge_asgi_commit(self):
        response = self.response()
        body = await anext(response.body_iterator)
        self.assertEqual(body, serialise(self.events[0]))
        self.assertEqual((self.history.observed_count, self.history.committed_count), (1, 0))
        self.assertEqual((self.state.tokens, self.state.output_started), (0, False))
        self.assertEqual(self.budget.used, self.quota)
        await response.aclose()
        await response.aclose()
        self.assert_released("cancelled")

    async def test_abandoned_response_before_iteration_releases_history_once(self):
        response = self.response()
        await response.aclose()
        await response.aclose()
        self.assertEqual((self.history.observed_count, self.history.committed_count), (0, 0))
        self.assert_released("cancelled")

    async def test_legacy_asgi_disconnect_cancels_blocked_group_without_commit(self):
        entered = asyncio.Event()
        held_during_unwind = []

        async def receive():
            await entered.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    held_during_unwind.append(self.budget.used)

        await asyncio.wait_for(self.start(self.response(), send, receive=receive, spec="2.3"), 2)
        self.assertEqual(held_during_unwind, [self.quota])
        self.assertEqual((self.history.committed_count, self.state.tokens), (0, 0))
        self.assert_released("cancelled")
