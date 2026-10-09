"""Check each queue wait's bound, predictive pricing while queued, holds and seat handoff."""

import asyncio
import time
from dataclasses import replace
from unittest.mock import patch

from narwhal.serving import dispatch, execution
from narwhal.serving.policy import ServingPolicy
from narwhal.serving.seats import decode_seats
from narwhal.types import Phase, Request, Role
from tests.serving.test_http_accounting import HttpHarness

STANDBY = {"type": "standby", "code": "standby"}


class QueueWaitHarness(HttpHarness):
    """One prefill seat on the producer, so a second request waits for it."""

    def setUp(self):
        super().setUp()
        self.cfg.serving = ServingPolicy(queue_capacity=4, queue_timeout_s=30.0)
        seats = patch.object(dispatch, "prefill_seats", return_value=1)
        seats.start()
        self.addCleanup(seats.stop)

    async def until(self, predicate):
        """Yield to the event loop until `predicate` holds."""
        for _ in range(400):
            if predicate():
                return
            await asyncio.sleep(0.005)
        self.fail("condition not reached")

    def submit(self, client):
        return asyncio.create_task(self.post(client))

    async def hold_prefill(self, client):
        """Start one request and wait until its prefill leg holds the producer's seat."""
        self.blocked = asyncio.Event()
        self.started.clear()
        task = self.submit(client)
        await asyncio.wait_for(self.started.wait(), timeout=2)
        return task

    def queued(self, phase):
        return len(self.router.dispatcher.queues[phase])

    def fill_decode(self):
        """Hold every decode seat on e3 with resident requests; return their release."""
        held = [f"held{index}" for index in range(decode_seats(self.router, "e3"))]
        for rid in held:
            self.router.monitor.dispatched("e3", Request(rid, 5, phase=Phase.DECODE))

        def release(count=None):
            for _ in range(len(held) if count is None else count):
                self.router.monitor.finished("e3", held.pop(0))

        return release

    def row(self, rid):
        return next(row for row in self.terminal_rows() if row["rid"] == rid)

    def assert_held_503(self, response, reason):
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.headers["retry-after"], "1")
        self.assertEqual(response.json()["error"], {"message": reason, **STANDBY})
        row = self.row(response.headers["x-request-id"])
        self.assertEqual(
            (row["terminal"], row["reason"], row["status"], row["error_type"]),
            ("rejected", "not_ready", 503, "standby"),
        )
        self.assertEqual(row["readiness_reason"], reason)


class WaitBoundTests(QueueWaitHarness):
    async def test_a_prefill_seat_wait_ends_at_the_remaining_queue_timeout(self):
        self.cfg.max_connections = 2
        self.cfg.serving = ServingPolicy(queue_capacity=4, queue_timeout_s=0.3)
        client = self.client()
        holder = await self.hold_prefill(client)
        seated = self.submit(client)
        await self.until(lambda: self.queued(Phase.PREFILL) == 1)
        late = self.submit(client)
        await self.until(lambda: len(self.router.admission_queue) == 1)
        await asyncio.sleep(0.1)
        # The cancelled request's admission seat passes to the late request, which then
        # waits for the prefill seat with what remains of its queue budget.
        seated.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await seated
        response = await asyncio.wait_for(late, timeout=2)
        self.assertEqual(response.status_code, 504, response.text)
        self.assertEqual(response.json()["error"]["type"], "expired")
        row = self.row(response.headers["x-request-id"])
        self.assertEqual((row["terminal"], row["reason"]), ("expired", "queue_timeout"))
        waits = row["queue_waits"]
        self.assertGreaterEqual(waits["admission"], 0.1)
        self.assertLess(waits["prefill"], 0.25)
        self.assertGreaterEqual(waits["admission"] + waits["prefill"], 0.29)
        self.assertLess(waits["admission"] + waits["prefill"], 0.45)
        self.blocked.set()
        self.assertEqual((await holder).status_code, 200)
        self.assert_released()


class PredictiveQueueTests(QueueWaitHarness):
    def setUp(self):
        super().setUp()
        self.cfg.admission = "predictive"

    async def test_a_queued_request_is_refused_when_its_projected_ttft_passes_the_budget(self):
        self.cfg.slo = replace(self.cfg.slo, ttft_s=0.3)
        client = self.client()
        holder = await self.hold_prefill(client)
        with patch.object(execution, "price_waiting", wraps=execution.price_waiting) as priced:
            response = await asyncio.wait_for(self.post(client), timeout=2)
        self.assertEqual(response.status_code, 429, response.text)
        self.assertEqual(response.headers["retry-after"], "1")
        row = self.row(response.headers["x-request-id"])
        self.assertEqual((row["refused_cause"], row["reason"]), ("queue", "queue"))
        # Priced on entry, then again when the time waited alone reaches the budget.
        self.assertGreaterEqual(priced.call_count, 2)
        self.assertGreater(row["admission_price"]["price_s"], 0.3)
        self.assertGreaterEqual(row["queue_waits"]["prefill"], 0.2)
        self.assertLess(row["queue_waits"]["prefill"], 1.0)
        self.blocked.set()
        self.assertEqual((await holder).status_code, 200)
        self.assert_released()

    async def test_an_admission_wait_is_priced_and_retry_after_excludes_the_wait(self):
        self.cfg.max_connections = 1
        client = self.client()
        holder = await self.hold_prefill(client)
        offset = [0.0]
        self.router._clock = lambda: time.monotonic() + offset[0]
        scheduler = self.router.scheduler
        with (
            patch.object(scheduler, "prefill_admission_price", return_value=9.5),
            patch.object(scheduler, "cheapest_own_prefill", return_value=1.0),
        ):
            waiting = self.submit(client)
            await self.until(lambda: len(self.router.admission_queue) == 1)
            # Waiting 3 s more prices the request at 12.5 s against the 10 s budget.
            offset[0] += 3.0
            self.router.wake_waiters()
            response = await asyncio.wait_for(waiting, timeout=2)
        self.assertEqual(response.status_code, 429, response.text)
        row = self.row(response.headers["x-request-id"])
        self.assertEqual(row["refused_cause"], "queue")
        self.assertGreaterEqual(row["admission_price"]["elapsed_s"], 3.0)
        self.assertGreater(row["admission_price"]["price_s"], 12.5)
        self.assertIsNone(row["queue_waits"]["prefill"])
        # The placement alone fits the budget, so a fresh request could be admitted now.
        self.assertEqual(response.headers["retry-after"], "1")
        self.blocked.set()
        self.assertEqual((await holder).status_code, 200)

        def slow_price(request, inst):
            offset[0] += 3.0
            return 12.0

        with (
            patch.object(scheduler, "prefill_admission_price", side_effect=slow_price),
            patch.object(scheduler, "cheapest_own_prefill", return_value=1.0),
        ):
            response = await self.post(client)
        self.assertEqual(response.status_code, 429, response.text)
        # 12 s of placement price against the 10 s budget; the 3 s waited is excluded.
        self.assertEqual(response.headers["retry-after"], "2")
        self.assert_released()


class SeatHandoffTests(QueueWaitHarness):
    async def test_a_freed_admission_seat_goes_to_the_waiter_in_the_same_pass(self):
        self.cfg.max_connections = 1
        self.cfg.serving = ServingPolicy(queue_capacity=1, queue_timeout_s=30.0)
        client = self.client()
        holder = await self.hold_prefill(client)
        waiting = self.submit(client)
        await self.until(lambda: len(self.router.admission_queue) == 1)
        # With every seat held and the queue full, an arrival meets the in-flight limit.
        full = await self.post(client)
        self.assertEqual(full.status_code, 429)
        self.assertEqual(full.json()["error"]["message"], "router in-flight limit reached")
        seen = {}
        write = self.router.journal.write

        def observe(row):
            if row.get("terminal") == "completed" and not seen:
                seen.update(inflight=self.router.inflight, queued=len(self.router.admission_queue))
            write(row)

        self.router.journal.write = observe
        self.blocked.set()
        self.assertEqual((await holder).status_code, 200)
        # When the holder's row is written, the waiter already holds the released seat.
        self.assertEqual(seen, {"inflight": 1, "queued": 0})
        self.assertEqual((await waiting).status_code, 200)
        self.assert_released()

    async def test_a_freed_prefill_seat_goes_to_the_waiter_in_the_same_pass(self):
        client = self.client()
        holder = await self.hold_prefill(client)
        waiting = self.submit(client)
        await self.until(lambda: self.queued(Phase.PREFILL) == 1)
        producer = self.router.monitor.instances["e0"]
        first_token = self.router.monitor.first_token
        seen = []

        def observe(iid, rid):
            first_token(iid, rid)
            if not seen:
                seen.append((len(producer.prefill), self.queued(Phase.PREFILL)))

        self.router.monitor.first_token = observe
        self.blocked.set()
        responses = await asyncio.gather(holder, waiting)
        self.assertEqual([r.status_code for r in responses], [200, 200])
        # The holder's prefill completion seats the waiter before it returns.
        self.assertEqual(seen, [(1, 0)])
        self.assert_released()

    async def test_a_waiter_cancelled_after_its_grant_releases_the_seat(self):
        client = self.client()
        serving = []
        serve = self.router.serve

        async def tracked(*args, **kwargs):
            serving.append(asyncio.current_task())
            return await serve(*args, **kwargs)

        self.router.serve = tracked
        holder = await self.hold_prefill(client)
        waiting = self.submit(client)
        await self.until(lambda: self.queued(Phase.PREFILL) == 1)
        producer = self.router.monitor.instances["e0"]
        first_token = self.router.monitor.first_token
        granted = []

        def observe(iid, rid):
            first_token(iid, rid)
            if not granted:
                granted.extend(producer.prefill)
                # The waiter's request is cancelled before its granted wait resumes.
                serving[1].cancel()

        self.router.monitor.first_token = observe
        self.blocked.set()
        self.assertEqual((await holder).status_code, 200)
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        self.assertEqual(len(granted), 1)
        # The waiter never reached its prefill leg, and its reservation left the producer.
        prefills = [
            body
            for _, body in self.calls
            if (body.get("kv_transfer_params") or {}).get("do_remote_decode")
        ]
        self.assertEqual(len(prefills), 1)
        self.assertEqual(producer.prefill, {})
        self.assertEqual((self.router.served, self.router.cancelled), (1, 1))
        self.assertEqual(self.row(granted[0])["terminal"], "cancelled")
        self.assert_released()


class HoldTests(QueueWaitHarness):
    async def start_waiters(self):
        """Start a prefilled, a prefilling, a prefill-seat and an admission waiter."""
        self.cfg.max_connections = 3
        client = self.client()
        release_decode = self.fill_decode()
        prefilled = self.submit(client)
        await self.until(lambda: self.queued(Phase.DECODE) == 1)
        prefilling = await self.hold_prefill(client)
        seat = self.submit(client)
        await self.until(lambda: self.queued(Phase.PREFILL) == 1)
        admission = self.submit(client)
        await self.until(lambda: len(self.router.admission_queue) == 1)
        return release_decode, prefilled, prefilling, seat, admission

    async def test_a_lifecycle_hold_ends_queued_and_prefilled_requests_with_503(self):
        release_decode, prefilled, prefilling, seat, admission = await self.start_waiters()
        reason = "whole-wave drain wave-test"
        self.router.lifecycle_blocked = reason
        for task in (prefilled, seat, admission):
            self.assert_held_503(await asyncio.wait_for(task, timeout=2), reason)
        # A request in its prefill leg ends the same way once prefill completes.
        self.blocked.set()
        self.assert_held_503(await asyncio.wait_for(prefilling, timeout=2), reason)
        self.assertEqual(self.router.outcome_reasons["rejected"]["not_ready"], 4)
        release_decode()
        self.router.lifecycle_blocked = ""
        self.assert_released()

    async def test_a_whole_wave_hold_during_sizing_ends_the_request_with_503(self):
        client = self.client()
        size = self.router.sizer.size
        reason = "whole-wave drain wave-test"

        async def drain_while_sizing(body):
            # The wave drains every engine before the request reaches placement.
            for iid in self.router.monitor.instances:
                self.router.scheduler.drain(iid)
            self.router.lifecycle_blocked = reason
            return await size(body)

        with patch.object(self.router.sizer, "size", side_effect=drain_while_sizing):
            response = await self.post(client)
        self.assert_held_503(response, reason)
        self.router.lifecycle_blocked = ""
        self.router.scheduler.draining.clear()
        self.assert_released()

    async def test_degraded_monitoring_ends_prefill_waits_and_lets_prefilled_requests_decode(self):
        release_decode, prefilled, prefilling, seat, admission = await self.start_waiters()
        monitoring = self.router.monitoring
        monitoring.fail("health", RuntimeError("probe failed"))
        monitoring.finish_pass(limit=1)
        reason = "monitoring degraded: health RuntimeError"
        for task in (seat, admission):
            self.assert_held_503(await asyncio.wait_for(task, timeout=2), reason)
        self.assertFalse(prefilled.done())
        # A freed decode seat goes to the prefilled request while monitoring stays degraded.
        release_decode(1)
        self.assertEqual((await asyncio.wait_for(prefilled, timeout=2)).status_code, 200)
        self.blocked.set()
        self.assertEqual((await asyncio.wait_for(prefilling, timeout=2)).status_code, 200)
        release_decode()
        self.assert_released()

    async def test_a_lost_lease_ends_prefill_waits_and_lets_prefilled_requests_decode(self):
        release_decode, prefilled, prefilling, seat, admission = await self.start_waiters()
        self.router.standby = True
        self.router.failover_blocked = "lease renewal failed"
        for task in (seat, admission):
            self.assert_held_503(await asyncio.wait_for(task, timeout=2), "lease renewal failed")
        self.assertFalse(prefilled.done())
        release_decode(1)
        self.assertEqual((await asyncio.wait_for(prefilled, timeout=2)).status_code, 200)
        self.blocked.set()
        self.assertEqual((await asyncio.wait_for(prefilling, timeout=2)).status_code, 200)
        release_decode()
        self.assert_released()


class ProducerDecodeTests(QueueWaitHarness):
    async def test_a_decode_seat_wait_skips_its_producer_after_a_role_change(self):
        client = self.client()
        release_decode = self.fill_decode()
        waiting = self.submit(client)
        await self.until(lambda: self.queued(Phase.DECODE) == 1)
        producer = self.router.monitor.instances["e0"]
        # Role control moves the producer to decode, where it has free seats.
        producer.role = Role.DECODE
        self.router.dispatcher.notify()
        await asyncio.sleep(0.01)
        self.assertEqual(self.queued(Phase.DECODE), 1)
        self.assertFalse(producer.decode)
        release_decode(1)
        response = await asyncio.wait_for(waiting, timeout=2)
        self.assertEqual(response.status_code, 200, response.text)
        row = self.row(response.headers["x-request-id"])
        self.assertEqual((row["prefill_iid"], row["decode_iid"]), ("e0", "e3"))
        release_decode()
        self.assert_released()

    async def test_decode_lands_on_the_producer_only_when_no_other_engine_can_take_it(self):
        self.cfg.serving = ServingPolicy()
        client = self.client()
        scheduler = self.router.scheduler
        schedule = scheduler.schedule
        producer = self.router.monitor.instances["e0"]
        eject = False

        def change_before_decode(request, **kwargs):
            # Role control moves the producer to decode after its prefill completes.
            if request.phase is Phase.DECODE:
                producer.role = Role.DECODE
                if eject:
                    scheduler.eject("e3", "liveness")
            return schedule(request, **kwargs)

        scheduler.schedule = change_before_decode
        for eject, decode_iid in ((False, "e3"), (True, "e0")):
            with self.subTest(only_producer=eject):
                producer.role = Role.PREFILL
                response = await self.post(client)
                self.assertEqual(response.status_code, 200, response.text)
                row = self.row(response.headers["x-request-id"])
                self.assertEqual((row["prefill_iid"], row["decode_iid"]), ("e0", decode_iid))
                self.assert_released()
