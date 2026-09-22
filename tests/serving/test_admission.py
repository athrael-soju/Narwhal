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

    async def test_fifo_full_queue_and_successor_notification(self):
        """The oldest waiter reserves first and wakes its successor on exit."""
        available = []
        calls = []

        def reserve(name):
            calls.append(name)
            return available.pop(0) if available else None

        first = await self.enqueue(lambda: reserve("first"))
        second = await self.enqueue(lambda: reserve("second"))
        self.assertEqual(calls, ["first", "first"])
        with self.assertRaises(QueueFull):
            await self.queue.acquire(lambda: reserve("third"), deadline=100)
        available.extend(["slot-a", "slot-b"])
        self.queue.notify()
        self.assertEqual(await first, "slot-a")
        self.assertEqual(await second, "slot-b")
        self.assertEqual(calls[-2:], ["first", "second"])
        self.assertEqual(len(self.queue), 0)
        self.assertEqual(self.queue.high_water, 2)

    async def test_cancelled_head_hands_capacity_to_successor(self):
        """Cancelling a notified head releases its queue position."""
        slots = []
        reserve = lambda: slots.pop() if slots else None
        first = await self.enqueue(reserve)
        second = await self.enqueue(reserve)
        slots.append("slot")
        self.queue.notify()
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
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
                with self.assertRaises(QueueExpired):
                    await task
                self.assertEqual(len(self.queue), 0)

    async def test_transport_wait_timeout_removes_waiter(self):
        """The event-loop timeout cleans up a queue with a stationary test clock."""
        queue = AdmissionQueue(1, 0.001, clock=lambda: 0)
        with self.assertRaises(QueueExpired):
            await queue.acquire(lambda: None, deadline=1)
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
