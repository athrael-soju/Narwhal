import tempfile
import time
import unittest
from types import SimpleNamespace

from narwhal.serving import execution
from narwhal.types import Phase, Request, Role
from tests.fixtures import role_scheduler
from tests.serving.test_http_accounting import HttpHarness
from tests.wire import FakeRendezvous, RendezvousLegs, engine_transports


class RendezvousServingTests(HttpHarness):
    def setUp(self):
        super().setUp()
        self.legs = RendezvousLegs(self.decode_frames)

    def transports(self):
        return engine_transports(self.legs)

    def client(self):
        client = super().client()
        self.router.engines.kv = FakeRendezvous()
        return client

    async def test_both_legs_run_concurrently_with_one_rendezvous(self):
        client = self.client()
        response = await self.post(client)
        self.assertEqual(response.status_code, 200, response.text)
        legs = {body["leg"]: (dict(request.headers), body) for request, body in self.legs.calls}
        self.assertEqual(legs["prefill"][1]["room"], legs["decode"][1]["room"])
        self.assertEqual(legs["prefill"][1]["max_tokens"], 1)
        self.assertTrue(legs["decode"][1]["stream"])
        self.assertTrue(legs["prefill"][0]["x-request-id"].endswith("-prefill"))
        self.assertTrue(legs["decode"][0]["x-request-id"].endswith("-decode"))
        self.assert_released()

    async def test_decode_without_its_prefill_ends_at_the_wait_bound(self):
        self.legs.prefill_status = 500
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
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        scheduler = role_scheduler(folder.name, {"d1": Role.DECODE, "d2": Role.DECODE})
        self.monitor = scheduler.monitor
        self.router = SimpleNamespace(
            scheduler=scheduler,
            profiles=scheduler.profiles,
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

    def test_a_decode_leg_skips_engines_without_a_free_seat(self):
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
