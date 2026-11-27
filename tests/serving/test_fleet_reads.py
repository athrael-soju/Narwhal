"""Check that only the router holding the lease answers the routes that describe the fleet."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx

from narwhal.runtime.lease import FileLease
from narwhal.serving import app as serving_app
from narwhal.serving import engines_api
from narwhal.serving.app import create_app
from tests.fixtures import fleet

FLEET_READS = {
    "/narwhal/engines": "narwhal.membership",
    "/narwhal/lifecycle": "narwhal.lifecycle",
    "/narwhal/state": "narwhal.state",
    "/narwhal/handoff": "narwhal.handoff",
    "/narwhal/targets": None,
}
# The reads that answer a diagnostic request on a router that does not control the fleet.
DIAGNOSTIC_READS = ("/narwhal/state", "/narwhal/lifecycle")
WRITES = (
    ("post", "/narwhal/engines", {"json": {"iid": "e6", "url": "http://engine-6.invalid:8000"}}),
    ("delete", "/narwhal/engines/e1", {}),
    ("post", "/narwhal/lifecycle/drain", {"json": {"engines": ["e1"]}}),
    ("post", "/narwhal/lifecycle/readmit", {"json": {"engines": ["e1"]}}),
)


class FleetReadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.cfg = fleet(self.root, engines=("e0", "e1", "e3"))
        self.cfg.state_path = self.root / "handoff.json"
        self.app = create_app(self.cfg)
        self.router = self.app.state.router
        self.addAsyncCleanup(self.router.engines.aclose)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://router"
        )
        self.addAsyncCleanup(self.client.aclose)

    def assert_refused(self, response, reason):
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.headers["retry-after"], "1")
        self.assertEqual(
            response.json(),
            {
                "error": {
                    "message": "this router does not control the fleet",
                    "type": "standby",
                    "code": "standby",
                    "reason": reason,
                }
            },
        )

    async def test_the_active_router_answers_every_fleet_read(self):
        for path, schema in FLEET_READS.items():
            with self.subTest(path=path):
                response = await self.client.get(path)
                self.assertEqual(response.status_code, 200, response.text)
                if schema is None:
                    self.assertEqual(len(response.json()), 3)
                else:
                    self.assertEqual(response.json()["schema"], schema)

    async def test_a_standby_refuses_every_fleet_read_with_its_reason(self):
        self.router.standby = True
        for path in FLEET_READS:
            with self.subTest(path=path):
                self.assert_refused(await self.client.get(path), "shadowing")

    async def test_a_diagnostic_read_returns_the_standbys_state_and_lifecycle(self):
        self.router.standby = True
        self.router.failover_blocked = "no fresh handoff"
        for path in DIAGNOSTIC_READS:
            with self.subTest(path=path):
                response = await self.client.get(path, headers={"x-narwhal-diagnostic": "true"})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["schema"], FLEET_READS[path])
                refused = await self.client.get(path, headers={"x-narwhal-diagnostic": "false"})
                self.assert_refused(refused, "no fresh handoff")
        for path in set(FLEET_READS) - set(DIAGNOSTIC_READS):
            with self.subTest(path=path):
                response = await self.client.get(path, headers={"x-narwhal-diagnostic": "true"})
                self.assert_refused(response, "no fresh handoff")
        state = await self.client.get("/narwhal/state", headers={"x-narwhal-diagnostic": "1"})
        self.assertEqual(
            (state.json()["ha"]["standby"], state.json()["ha"]["blocked"]),
            (True, "no fresh handoff"),
        )

    async def test_the_reason_names_the_lost_control_not_another_readiness_cause(self):
        self.router.standby = True
        self.router.lifecycle_blocked = "whole-wave drain w1"
        self.assertEqual((await self.client.get("/ready")).json()["reason"], "whole-wave drain w1")
        self.assert_refused(await self.client.get("/narwhal/engines"), "shadowing")

    async def test_a_fenced_router_names_its_failover_block(self):
        self.router.standby = True
        self.router.failover_blocked = "lease renewal failed"
        for path in FLEET_READS:
            with self.subTest(path=path):
                self.assert_refused(await self.client.get(path), "lease renewal failed")

    async def test_a_router_whose_lease_expired_refuses_fleet_reads(self):
        self.router.lease = Mock(valid=Mock(return_value=False))
        for path in FLEET_READS:
            with self.subTest(path=path):
                self.assert_refused(await self.client.get(path), "lease expired")

    async def test_a_lease_holder_that_yields_for_shutdown_refuses_fleet_reads(self):
        lease = FileLease(self.root / "router.lease", "router-a", 30)
        self.assertTrue(lease.claim())
        self.addCleanup(lease.release)
        app = create_app(self.cfg, lease=lease)
        self.addAsyncCleanup(app.state.router.engines.aclose)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router"
        ) as client:
            self.assertEqual((await client.get("/narwhal/engines")).status_code, 200)
            await app.state.yield_control()
            for path in FLEET_READS:
                with self.subTest(path=path):
                    self.assert_refused(await client.get(path), "shutting down")

    async def in_rotation(self) -> bool:
        """Apply the shipped HAProxy health check: control of the fleet, identities validated."""
        if (await self.client.get("/narwhal/targets")).status_code != 200:
            return False
        return "engine identity validation pending" not in (await self.client.get("/ready")).text

    async def test_a_lease_holder_that_is_not_ready_stays_in_rotation(self):
        self.router.lifecycle.identities_ready = True
        self.assertTrue(await self.in_rotation())
        scheduler = self.router.scheduler
        not_ready = {
            "whole-wave hold": ("_lifecycle_blocked", self.router, "whole-wave drain w1"),
            "degraded monitoring": ("degraded", self.router.monitoring, "ConnectError:metrics"),
            "no live engines": ("live_instances", scheduler, Mock(return_value=[])),
        }
        for state, (name, owner, value) in not_ready.items():
            with self.subTest(state=state), patch.object(owner, name, value):
                self.assertEqual((await self.client.get("/ready")).status_code, 503)
                self.assertTrue(await self.in_rotation())

    async def test_a_router_validating_identities_or_without_the_lease_leaves_rotation(self):
        self.router.lifecycle.identities_ready = True
        self.assertTrue(await self.in_rotation())
        self.router.lifecycle.identities_ready = False
        self.assertFalse(await self.in_rotation())
        self.router.lifecycle.identities_ready = True
        self.router.standby = True
        self.assertFalse(await self.in_rotation())

    async def test_health_ready_and_metrics_answer_on_a_standby(self):
        self.router.standby = True
        health = await self.client.get("/health")
        self.assertEqual((health.status_code, health.json()["status"]), (200, "standby"))
        ready = await self.client.get("/ready")
        self.assertEqual((ready.status_code, ready.json()["reason"]), (503, "shadowing"))
        metrics = await self.client.get("/metrics")
        self.assertEqual(metrics.status_code, 200)
        self.assertIn("narwhal_served_total", metrics.text)
        self.assertEqual((await self.client.get("/v1/models")).status_code, 200)

    async def test_a_standby_refuses_every_write_before_it_acts(self):
        self.router.standby = True
        with (
            patch.object(engines_api, "join_engine", AsyncMock()) as join,
            patch.object(serving_app, "capture_process_identities", AsyncMock()) as capture,
        ):
            for method, path, kwargs in WRITES:
                with self.subTest(path=path, method=method):
                    response = await self.client.request(method.upper(), path, **kwargs)
                    self.assertEqual(response.status_code, 503, response.text)
                    self.assertEqual(response.json()["error"], engines_api.NOT_CONTROLLING)
        join.assert_not_awaited()
        capture.assert_not_awaited()
        self.assertEqual(self.router.membership.generation, 0)
        self.assertEqual(self.router.lifecycle.records, {})

    async def test_openapi_lists_the_refusal_of_each_fleet_read(self):
        paths = (await self.client.get("/openapi.json")).json()["paths"]
        for path in FLEET_READS:
            with self.subTest(path=path):
                self.assertIn("503", paths[path]["get"]["responses"])
                headers = {
                    parameter["name"]
                    for parameter in paths[path]["get"].get("parameters", [])
                    if parameter["in"] == "header"
                }
                expected = {"x-narwhal-diagnostic"} if path in DIAGNOSTIC_READS else set()
                self.assertEqual(headers, expected)


if __name__ == "__main__":
    unittest.main()
