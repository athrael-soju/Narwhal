import asyncio
import itertools
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

from narwhal.engines.connector import RendezvousConnector
from narwhal.profiling.store import ProfileStore
from narwhal.scheduling.control import SLO, Thresholds
from narwhal.scheduling.monitor import InstanceMonitor
from narwhal.scheduling.scheduler.placement import GlobalScheduler
from narwhal.serving import execution
from narwhal.types import Instance, Phase, Request, Role
from tests.fixtures import profile
from tests.serving.test_http_accounting import HttpHarness


class FakeRendezvous(RendezvousConnector):
    name = "fake"
    decode_wait_s = 0.3

    def __init__(self):
        self.rooms = itertools.count()

    def handoff_bound(self, lease_s):
        return float(lease_s)

    def rendezvous(self, producer):
        return {"room": next(self.rooms)}

    def prefill_body(self, body, rendezvous):
        return {**body, "leg": "prefill", **rendezvous}

    def decode_body(self, body, rendezvous):
        return {**body, "leg": "decode", **rendezvous}


class RendezvousServingTests(HttpHarness):
    def setUp(self):
        super().setUp()
        self.prefill_seen = asyncio.Event()
        self.decode_seen = asyncio.Event()
        self.prefill_status = 200

    async def engine(self, request):
        body = json.loads(request.content)
        self.calls.append((dict(request.headers), body))
        if body.get("leg") == "prefill":
            self.prefill_seen.set()
            # Each leg waits for the other, so only concurrent dispatch completes.
            await asyncio.wait_for(self.decode_seen.wait(), 5)
            return httpx.Response(self.prefill_status, json={})
        self.decode_seen.set()
        if self.prefill_status != 200:
            await asyncio.Event().wait()
        await asyncio.wait_for(self.prefill_seen.wait(), 5)
        wire = "".join("data: " + json.dumps(frame) + "\n\n" for frame in self.decode_frames)
        return httpx.Response(200, text=wire + "data: [DONE]\n\n")

    def client(self):
        client = super().client()
        self.router.engines.kv = FakeRendezvous()
        return client

    async def test_both_legs_run_concurrently_with_one_rendezvous(self):
        client = self.client()
        response = await self.post(client)
        self.assertEqual(response.status_code, 200, response.text)
        legs = {body["leg"]: (headers, body) for headers, body in self.calls}
        self.assertEqual(legs["prefill"][1]["room"], legs["decode"][1]["room"])
        self.assertEqual(legs["prefill"][1]["max_tokens"], 1)
        self.assertTrue(legs["decode"][1]["stream"])
        self.assertTrue(legs["prefill"][0]["x-request-id"].endswith("-prefill"))
        self.assertTrue(legs["decode"][0]["x-request-id"].endswith("-decode"))
        self.assert_released()

    async def test_decode_without_its_prefill_ends_at_the_wait_bound(self):
        self.prefill_status = 500
        client = self.client()
        began = time.monotonic()
        response = await self.post(client)
        self.assertLess(time.monotonic() - began, 2)
        self.assertGreaterEqual(response.status_code, 500, response.text)
        self.assertEqual(response.json()["error"]["type"], "prefill")
        row = self.terminal_rows()[-1]
        self.assertIn("prefill leg", row["error"])
        for failure in row["attempt_failures"]:
            self.assertEqual(failure["phase"], "prefill")
        self.assert_released()


class SeatedDecodeTests(unittest.TestCase):
    def setUp(self):
        with tempfile.TemporaryDirectory() as folder:
            profiles = ProfileStore(Path(folder) / "profiles.json")
        self.monitor = InstanceMonitor(profiles=profiles)
        for iid in ("d1", "d2"):
            profiles.put(profile(iid))
            self.monitor.add(Instance(iid, f"http://{iid}", Role.DECODE))
        scheduler = GlobalScheduler(self.monitor, profiles, SLO(2.0, 0.05), Thresholds())
        self.router = SimpleNamespace(
            scheduler=scheduler,
            profiles=profiles,
            monitor=self.monitor,
            sequence_limits={"d1": 2, "d2": 2},
            controller=SimpleNamespace(
                demand=SimpleNamespace(output_estimator=lambda: lambda r: r.wanted_len)
            ),
        )
        self.request = Request("new", 128, wanted_len=64)

    def resident(self, iid, rid, left):
        request = Request(rid, 128, phase=Phase.DECODE, output_len=8, wanted_len=8 + left)
        self.monitor.instances[iid].decode[rid] = request

    def skipped(self, exclude=frozenset()):
        return execution._seated_decode_engines(self.router, self.request, set(exclude))

    def test_a_rendezvous_decode_leg_skips_engines_without_a_free_seat(self):
        self.resident("d1", "r1", 100)
        self.resident("d1", "r2", 100)
        self.assertEqual(self.skipped(), {"d1"})
        self.resident("d2", "r3", 100)
        self.assertEqual(self.skipped(), {"d1"})
        self.assertEqual(self.skipped({"d1"}), set())

    def test_with_every_seat_taken_the_leg_goes_where_a_seat_frees_first(self):
        self.resident("d1", "r1", 400)
        self.resident("d1", "r2", 300)
        self.resident("d2", "r3", 500)
        self.resident("d2", "r4", 20)
        self.assertEqual(self.skipped(), {"d1"})
        # Legs already queued on d2 take its next freed seats.
        self.resident("d2", "r5", 10)
        self.resident("d2", "r6", 600)
        self.assertEqual(self.skipped(), {"d2"})


if __name__ == "__main__":
    unittest.main()
