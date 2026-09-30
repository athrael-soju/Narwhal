"""Check the router's event-loop lag measurement."""

import asyncio
import random
import statistics
import time
import unittest
from types import SimpleNamespace

from narwhal.serving.saturation import LAG_PROBE_S, RecentDelays, measure_loop_lag


class LoopLagTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_blocked_loop_records_its_lateness(self):
        router = SimpleNamespace(loop_lag_s=0.0)
        task = asyncio.create_task(measure_loop_lag(router))
        self.addCleanup(task.cancel)
        await asyncio.sleep(3 * LAG_PROBE_S)
        self.assertLess(router.loop_lag_s, 0.1)
        time.sleep(0.3)
        await asyncio.sleep(0.01)
        self.assertGreater(router.loop_lag_s, 0.2)
        await asyncio.sleep(3 * LAG_PROBE_S)
        self.assertLess(router.loop_lag_s, 0.1)


class RecentDelayTests(unittest.TestCase):
    def test_the_median_covers_only_the_trailing_window(self):
        now = [0.0]
        delays = RecentDelays(2.0, lambda: now[0])
        self.assertEqual(delays.median(), 0.0)
        for delay in (0.1, 0.9, 3.0):
            delays.add(delay)
        self.assertEqual(delays.median(), 0.9)
        now[0] = 1.5
        delays.add(0.2)
        self.assertAlmostEqual(delays.median(), 0.55)
        now[0] = 2.5
        self.assertEqual(delays.median(), 0.2)
        now[0] = 10.0
        self.assertEqual(delays.median(), 0.0)

    def test_the_median_waits_for_its_minimum_sample_count(self):
        now = [0.0]
        delays = RecentDelays(2.0, lambda: now[0], min_samples=3)
        delays.add(5.0)
        delays.add(5.0)
        self.assertEqual(delays.median(), 0.0)
        delays.add(0.1)
        self.assertEqual(delays.median(), 5.0)
        now[0] = 3.0
        delays.add(5.0)
        self.assertEqual(delays.median(), 0.0)

    def test_the_median_matches_a_full_recomputation_as_rows_expire(self):
        rng = random.Random(7)
        now = [0.0]
        delays = RecentDelays(2.0, lambda: now[0])
        rows: list[tuple[float, float]] = []
        for _ in range(2000):
            now[0] += rng.uniform(0.0, 0.05)
            delay = rng.choice((rng.uniform(0.0, 1.0), 0.5))
            delays.add(delay)
            rows.append((now[0], delay))
            window = [d for at, d in rows if at >= now[0] - 2.0]
            self.assertEqual(delays.median(), statistics.median(window))
