"""Keep replay copies, terminal commitment and prediction inside their lifetimes."""

import asyncio
import unittest
import weakref
from dataclasses import replace
from unittest.mock import patch

from narwhal.serving.continuation import ContinuationHistory
from tests.engines.replay_fixtures import envelope, wire
from tests.serving import test_continuation_recovery as recovery_fixture


class ReplayPrompt(list):
    """Permit weak-reference ownership checks on the materialised replay list."""


class RecoveryLifecycleTests(unittest.IsolatedAsyncioTestCase):
    # Reuse the HTTP fixture without inheriting its separate acceptance tests.
    setUp = recovery_fixture.ContinuationRecoveryTests.setUp
    recovered_chunks = recovery_fixture.ContinuationRecoveryTests.recovered_chunks
    before_failure = recovery_fixture.ContinuationRecoveryTests.before_failure
    engine = recovery_fixture.ContinuationRecoveryTests.engine
    client = recovery_fixture.ContinuationRecoveryTests.client
    body = recovery_fixture.ContinuationRecoveryTests.body
    post = recovery_fixture.ContinuationRecoveryTests.post
    events = recovery_fixture.ContinuationRecoveryTests.events
    row = recovery_fixture.ContinuationRecoveryTests.row
    assert_released = recovery_fixture.ContinuationRecoveryTests.assert_released
    assert_terminal_error = recovery_fixture.ContinuationRecoveryTests.assert_terminal_error

    async def test_previous_replay_prompt_is_released_before_the_next_allocation(self):
        self.cfg.continuation = replace(self.cfg.continuation, max_attempts=2, recovery_budget=2)
        self.plans["e4"] = [([wire(envelope((8,), "B", prompt=(3, 7)))], True)]
        self.plans["e0"] = [
            (
                [
                    wire(envelope((9, 10, 11), "CDE", prompt=(3, 7, 8), finish="length")),
                    b"data: [DONE]\n\n",
                ],
                False,
            )
        ]
        make_prompt = ContinuationHistory.replay_prompt
        copies = []
        alive_at_allocation = []

        def replay_prompt(history):
            result = ReplayPrompt(make_prompt(history))
            if history.committed_count:
                alive_at_allocation.append(sum(reference() is not None for reference in copies))
                copies.append(weakref.ref(result))
            return result

        # The engine fixture records JSON-decoded request copies, never this list.
        with patch.object(ContinuationHistory, "replay_prompt", new=replay_prompt):
            response = await self.post(self.client())
        self.assertEqual(self.row()["terminal"], "completed", response.text)
        self.assertEqual(self.row()["continuation"]["attempts"], 2)
        self.assertEqual(alive_at_allocation, [0, 0])
        self.assertTrue(all(reference() is None for reference in copies))
        self.assert_released()

    async def test_qualification_time_rechecks_the_replay_prefill_estimate(self):
        self.cfg.admission = "predictive"
        self.cfg.request_timeout_s = 10.0
        client = self.client()
        real_clock = self.router._clock
        elapsed = 0.0
        checked = False
        self.router._clock = lambda: real_clock() + elapsed

        async def spend_deadline(iid, path):
            nonlocal elapsed, checked
            if self.failure_at is not None and path == "/metrics" and not checked:
                checked = True
                elapsed = 9.9

        def price(request, instance):
            return 0.001 if request.recovery_deadline is None else 1.0

        self.qualification_hook = spend_deadline
        with patch.object(self.router.scheduler, "prefill_admission_price", side_effect=price):
            response = await self.post(client)
        self.assertTrue(checked)
        self.assert_terminal_error(response)
        self.assertEqual(self.row()["continuation"]["terminal_reason"], "prediction")
        self.assertEqual(self.row()["continuation"]["replay_input_tokens"], 0)
        self.assertEqual(len(self.prefills), 1)

    async def test_failed_prefill_releases_its_replay_prompt_before_retrying(self):
        self.cfg.continuation = replace(self.cfg.continuation, max_attempts=2, recovery_budget=2)
        self.recovery_prefill_status = 500
        engine = self.engine

        async def fail_one_prefill(request):
            response = await engine(request)
            if response.status_code == 500:
                self.recovery_prefill_status = 200
            return response

        self.engine = fail_one_prefill
        make_prompt = ContinuationHistory.replay_prompt
        copies = []
        alive_at_allocation = []

        def replay_prompt(history):
            result = ReplayPrompt(make_prompt(history))
            if history.committed_count:
                alive_at_allocation.append(sum(reference() is not None for reference in copies))
                copies.append(weakref.ref(result))
            return result

        with patch.object(ContinuationHistory, "replay_prompt", new=replay_prompt):
            response = await self.post(self.client())
        self.assertEqual(self.row()["terminal"], "completed", response.text)
        self.assertEqual(self.row()["continuation"]["attempts"], 2)
        self.assertEqual(alive_at_allocation, [0, 0])
        self.assertTrue(all(reference() is None for reference in copies))
        self.assertEqual([iid for iid, _, _ in self.decodes], ["e3", "e4"])
        self.assert_released()

    async def test_cancel_during_close_after_done_keeps_the_completed_outcome(self):
        await self.check_close_after_done(cancel=True)

    async def test_deadline_during_close_after_done_does_not_emit_another_terminal(self):
        await self.check_close_after_done(cancel=False)

    async def check_close_after_done(self, *, cancel):
        if not cancel:
            self.cfg.request_timeout_s = 0.1
        self.client()
        response = await self.router.serve("/v1/completions", self.body(), {})
        entered = asyncio.Event()
        bodies = []
        close = recovery_fixture.RecoveryStream.aclose

        async def block_terminal_close(stream):
            await close(stream)
            if len(self.streams) == 2 and stream is self.streams[-1]:
                entered.set()
                await asyncio.Event().wait()

        async def receive():
            await asyncio.Event().wait()

        async def send(message):
            if message["type"] == "http.response.body":
                bodies.append((message.get("body", b""), message.get("more_body", False)))

        with patch.object(recovery_fixture.RecoveryStream, "aclose", new=block_terminal_close):
            task = asyncio.create_task(
                response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
            )
            try:
                await asyncio.wait_for(entered.wait(), 2)
                self.assertTrue(response.lifecycle.continuation.terminal_committed)
                if cancel:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                else:
                    await asyncio.wait_for(task, 2)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await response.aclose()
        output = b"".join(body for body, _ in bodies)
        self.assertEqual(output.count(b"data: [DONE]\n\n"), 1)
        self.assertNotIn(b'"error"', output)
        if not cancel:
            self.assertEqual(bodies[-1], (b"", False))
        self.assertEqual(self.row()["terminal"], "completed")
        self.assertEqual(self.row()["continuation"]["terminal_reason"], "completed")
        self.assertEqual(self.row()["output_len"], 5)
        self.assertEqual(
            (self.router.served, self.router.cancelled, self.router.expired), (1, 0, 0)
        )
        self.assert_released()
