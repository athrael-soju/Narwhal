"""Check the router's event-loop lag measurement."""

import asyncio
import time
import unittest
from types import SimpleNamespace

from narwhal.serving.saturation import LAG_PROBE_S, measure_loop_lag


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
