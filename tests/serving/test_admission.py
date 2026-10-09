"""Check FIFO reservation, queue bounds and cancellation cleanup."""

import asyncio
import unittest

from narwhal.serving.admission import AdmissionQueue, QueueExpired, QueueFull


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    """An explicit clock and event-loop turns control waiting requests."""

    def setUp(self):
        self.now = 10.0
        self.queue = AdmissionQueue(2, 5, clock=lambda: self.now)
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def enqueue(self, reserve, deadline=100):
        """Start one waiter and yield until it reaches its first wait."""
        task = asyncio.create_task(self.queue.acquire(reserve, deadline=deadline))
        self.tasks.append(task)
        await asyncio.sleep(0)
        return task

    async def test_immediate_reservation_accepts_falsey_values(self):
        """A falsey reservation still owns capacity."""
        self.assertEqual(await self.queue.acquire(lambda: 0, deadline=11), 0)
        self.assertEqual(len(self.queue), 0)
        self.assertEqual(self.queue.high_water, 0)

    async def test_expired_arrival_leaves_reservation_untouched(self):
        """The original deadline is checked before calling the reserve callback."""
        calls = []
        with self.assertRaises(QueueExpired):
            await self.queue.acquire(lambda: calls.append(1), deadline=10)
        self.assertEqual(calls, [])

    async def test_fifo_full_queue_and_same_pass_handoff(self):
        """Freed capacity goes to waiters in FIFO order before the notifier returns."""
        available = []
        calls = []

        def reserve(name):
            calls.append(name)
            return available.pop(0) if available else None

        first = await self.enqueue(lambda: reserve("first"))
        second = await self.enqueue(lambda: reserve("second"))
        # Only an empty queue lets an arrival reserve ahead of the waiters.
        self.assertEqual(calls, ["first"])
        with self.assertRaises(QueueFull):
            await self.queue.acquire(lambda: reserve("third"), deadline=100)
        self.assertEqual(calls, ["first", "first"])
        available.extend(["slot-a", "slot-b"])
        self.queue.notify()
        self.assertEqual(calls[-2:], ["first", "second"])
        self.assertEqual(len(self.queue), 0)
        self.assertEqual(await first, "slot-a")
        self.assertEqual(await second, "slot-b")
        self.assertEqual(self.queue.high_water, 2)

    async def test_an_arrival_meets_a_full_queue_only_while_no_capacity_is_free(self):
        """A full queue hands out capacity freed without a notification before refusing."""
        slots = []
        reserve = lambda: slots.pop() if slots else None
        first = await self.enqueue(reserve)
        await self.enqueue(reserve)
        slots.append("slot")
        task = await self.enqueue(reserve)
        self.assertEqual(await first, "slot")
        self.assertFalse(task.done())
        self.assertEqual(len(self.queue), 2)

    async def test_a_cancelled_waiter_passes_its_position_to_the_successor(self):
        """Cancelling the head before a grant leaves the capacity for the next waiter."""
        slots = []
        reserve = lambda: slots.pop() if slots else None
        first = await self.enqueue(reserve)
        second = await self.enqueue(reserve)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        slots.append("slot")
        self.queue.notify()
        self.assertEqual(await second, "slot")
        self.assertEqual(len(self.queue), 0)

    async def test_cancelled_tail_preserves_head_order(self):
        """Tail cancellation releases one waiting seat while the head remains."""
        first = await self.enqueue(lambda: None)
        second = await self.enqueue(lambda: None)
        second.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await second
        self.assertEqual(len(self.queue), 1)
        self.assertFalse(first.done())

    async def test_queue_and_request_deadlines_choose_the_earlier_limit(self):
        """A notification at either effective deadline expires the waiter."""
        for deadline, expires in ((12, 12), (100, 15)):
            with self.subTest(deadline=deadline):
                self.now = 10
                task = await self.enqueue(lambda: None, deadline)
                self.now = expires
                self.queue.notify()
                with self.assertRaises(QueueExpired) as caught:
                    await task
                self.assertEqual(caught.exception.at_deadline, deadline == expires)
                self.assertEqual(len(self.queue), 0)

    async def test_transport_wait_timeout_removes_waiter(self):
        """The event-loop timeout cleans up a queue with a stationary test clock."""
        queue = AdmissionQueue(1, 0.001, clock=lambda: 0)
        with self.assertRaises(QueueExpired) as caught:
            await queue.acquire(lambda: None, deadline=1)
        self.assertFalse(caught.exception.at_deadline)
        self.assertEqual(len(queue), 0)

    async def test_a_timer_ahead_of_the_queue_clock_still_reports_the_deadline(self):
        """The event-loop timer ends the wait while the queue clock reads before the deadline."""
        queue = AdmissionQueue(1, 30, clock=lambda: 0)
        with self.assertRaises(QueueExpired) as caught:
            await queue.acquire(lambda: None, deadline=0.001)
        self.assertTrue(caught.exception.at_deadline)
        self.assertEqual(len(queue), 0)

    async def test_callback_failure_releases_waiter(self):
        """A placement exception frees the head and wakes the next request."""
        ready = False

        def reserve():
            if ready:
                raise ValueError("placement failed")
            return None

        task = await self.enqueue(reserve)
        ready = True
        self.queue.notify()
        with self.assertRaisesRegex(ValueError, "placement failed"):
            await task
        self.assertEqual(len(self.queue), 0)

    async def test_wait_s_shortens_the_wait_and_zero_takes_only_free_capacity(self):
        """A waiter with no wait left reserves free capacity but never queues."""
        self.assertEqual(await self.queue.acquire(lambda: "slot", deadline=100, wait_s=0), "slot")
        with self.assertRaises(QueueExpired) as caught:
            await self.queue.acquire(lambda: None, deadline=100, wait_s=0)
        self.assertFalse(caught.exception.at_deadline)
        self.assertEqual(len(self.queue), 0)
        queue = AdmissionQueue(1, 30, clock=lambda: 0)
        with self.assertRaises(QueueExpired) as caught:
            await queue.acquire(lambda: None, deadline=1, wait_s=0.001)
        self.assertFalse(caught.exception.at_deadline)


class AdmissionCheckTests(unittest.IsolatedAsyncioTestCase):
    """A waiter's check runs on entry and on each wake, and can end its wait."""

    async def test_the_check_runs_on_entry_and_on_each_wake(self):
        queue = AdmissionQueue(2, 5, clock=lambda: 0)
        runs = []
        refuse = False

        def check():
            runs.append(1)
            if refuse:
                raise ValueError("over budget")
            return None

        first = asyncio.create_task(queue.acquire(lambda: None, deadline=10, check=check))
        second = asyncio.create_task(queue.acquire(lambda: None, deadline=10, check=check))
        await asyncio.sleep(0)
        self.assertEqual(len(runs), 2)
        refuse = True
        # Every waiter wakes, not only the head.
        queue.wake_all()
        for task in (first, second):
            with self.assertRaisesRegex(ValueError, "over budget"):
                await task
        self.assertEqual(len(runs), 4)
        self.assertEqual(len(queue), 0)

    async def test_a_check_at_entry_ends_the_wait_before_queueing(self):
        queue = AdmissionQueue(1, 5, clock=lambda: 0)

        def check():
            raise ValueError("refused")

        with self.assertRaisesRegex(ValueError, "refused"):
            await queue.acquire(lambda: None, deadline=10, check=check)
        self.assertEqual(len(queue), 0)

    async def test_a_due_recheck_runs_the_check_instead_of_expiring(self):
        now = [0.0]
        queue = AdmissionQueue(1, 30, clock=lambda: now[0])
        runs = []

        def check():
            runs.append(now[0])
            if len(runs) == 3:
                raise ValueError("over budget")
            # The router clock moves with each run; the next run is due 1 ms later.
            now[0] += 0.001
            return now[0] + 0.001

        with self.assertRaisesRegex(ValueError, "over budget"):
            await queue.acquire(lambda: None, deadline=10, check=check)
        self.assertEqual(len(runs), 3)
        self.assertEqual(len(queue), 0)

    async def test_a_recheck_later_than_the_expiry_leaves_the_expiry(self):
        queue = AdmissionQueue(1, 0.001, clock=lambda: 0)
        runs = []

        def check():
            runs.append(1)
            return 5.0

        with self.assertRaises(QueueExpired) as caught:
            await queue.acquire(lambda: None, deadline=10, check=check)
        self.assertFalse(caught.exception.at_deadline)
        self.assertEqual(runs, [1])
