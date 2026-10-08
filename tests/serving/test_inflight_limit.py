"""Check the router in-flight limit and the gauges behind its 429s."""

import asyncio
import re

from narwhal.serving.policy import ServingPolicy
from narwhal.serving.saturation import SATURATED_TTFT_SHARE
from tests.serving.test_http_accounting import HttpHarness


def gauge(text, name):
    """Return the value of an unlabelled gauge from a Prometheus exposition."""
    match = re.search(rf"^{name} (\S+)$", text, re.MULTILINE)
    assert match is not None, name
    return float(match[1])


class InflightLimitTests(HttpHarness):
    """Requests held in prefill fill the in-flight limit; the next arrival is rejected."""

    async def fill(self, client, held):
        """Start `held` requests and wait until each reaches its blocked prefill leg."""
        self.blocked = asyncio.Event()
        tasks = [asyncio.create_task(self.post(client)) for _ in range(held)]
        for _ in range(200):
            if self.router.inflight == held:
                break
            await asyncio.sleep(0.005)
        self.assertEqual(self.router.inflight, held)
        return tasks

    async def release(self, tasks):
        self.blocked.set()
        responses = await asyncio.gather(*tasks)
        self.assertEqual([r.status_code for r in responses], [200] * len(tasks))
        self.assert_released()

    async def test_the_limit_rejects_the_next_arrival_and_its_gauges_report_it(self):
        self.cfg.max_connections = 2
        client = self.client()
        tasks = await self.fill(client, 2)
        rejected = await self.post(client)
        self.assertEqual(rejected.status_code, 429)
        self.assertEqual(rejected.headers["retry-after"], "1")
        self.assertEqual(
            rejected.json()["error"],
            {"message": "router in-flight limit reached", "type": "server_overloaded_error"},
        )
        text = (await client.get("/metrics")).text
        self.assertEqual(gauge(text, "narwhal_admission_inflight"), 2)
        self.assertEqual(gauge(text, "narwhal_admission_inflight_limit"), 2)
        self.assertIn('narwhal_rejected_total{reason="inflight_limit"} 1', text)
        await self.release(tasks)
        self.assertEqual(self.terminal_rows()[-1]["reason"], None)

    async def test_queued_requests_extend_retention_but_not_the_limit(self):
        self.cfg.max_connections = 1
        self.cfg.serving = ServingPolicy(
            queue_capacity=1, queue_timeout_s=5.0, handoff_timeout_s=5.0
        )
        client = self.client()
        tasks = await self.fill(client, 1)
        tasks.append(asyncio.create_task(self.post(client)))
        for _ in range(200):
            if len(self.router.admission_queue) == 1:
                break
            await asyncio.sleep(0.005)
        rejected = await self.post(client)
        self.assertEqual(rejected.status_code, 429)
        self.assertEqual(rejected.json()["error"]["message"], "router in-flight limit reached")
        state = (await client.get("/narwhal/state")).json()
        self.assertEqual((state["admission"]["inflight"], state["admission"]["limit"]), (1, 1))
        self.assertEqual(state["admission"]["queued"], 1)
        self.assertEqual(state["serving"]["http_retained_limit"], 2)
        await self.release(tasks)

    async def test_metrics_report_the_signals_behind_the_saturation_429(self):
        client = self.client()
        router = self.router
        threshold = SATURATED_TTFT_SHARE * router.scheduler.slo.ttft_s
        router.loop_lag_s = threshold / 2
        for _ in range(8):
            router.sizing_delays.add(threshold / 4)
        self.assertEqual((await self.post(client)).status_code, 200)
        text = (await client.get("/metrics")).text
        self.assertEqual(gauge(text, "narwhal_saturation_threshold_seconds"), threshold)
        self.assertGreaterEqual(gauge(text, "narwhal_request_sizing_delay_seconds"), threshold / 4)
        state = (await client.get("/narwhal/state")).json()["admission"]
        self.assertEqual(state["saturation_threshold_s"], threshold)
        router.loop_lag_s = threshold
        text = (await client.get("/metrics")).text
        self.assertEqual(gauge(text, "narwhal_router_loop_lag_seconds"), threshold)
        saturated = await self.post(client)
        self.assertEqual(saturated.status_code, 429)
        self.assertIn("router saturated", saturated.json()["error"]["message"])
