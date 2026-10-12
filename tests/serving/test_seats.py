import math
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from narwhal.backends.vllm.identity import VllmIdentity
from narwhal.scheduling.scheduler.occupancy import decode_admits, decode_occupancy
from narwhal.serving.app import create_app
from narwhal.serving.policy import ServingPolicy
from narwhal.serving.seats import InputLengths, decode_seats, prefill_seats
from narwhal.types import Phase, Request, Role
from tests.fixtures import fleet
from tests.serving.test_http_accounting import HttpHarness


def launch(*args):
    return {"launch": {"args": list(args)}}


class AttestedSequenceLimitTests(unittest.TestCase):
    def test_the_launch_arguments_set_the_sequence_limit(self):
        for payload, expected in (
            (launch("--max-num-seqs", "48"), 48),
            (launch("--max-model-len", "4096", "--max-num-seqs=12"), 12),
            (launch("--max-model-len", "4096"), None),
            (launch("--max-num-seqs"), None),
            (launch("--max-num-seqs", "0"), None),
            (launch("--max-num-seqs", "many"), None),
            ({"launch": {"args": "--max-num-seqs 4"}}, None),
            ({}, None),
            (None, None),
        ):
            with self.subTest(payload=payload):
                self.assertEqual(VllmIdentity().sequence_limit(payload), expected)


class InputLengthTests(unittest.TestCase):
    def test_the_mean_covers_the_trailing_window(self):
        now = [0.0]
        lengths = InputLengths(10.0, lambda: now[0])
        self.assertIsNone(lengths.mean())
        lengths.add(100)
        now[0] = 6.0
        lengths.add(300)
        self.assertEqual(lengths.mean(), 200)
        now[0] = 12.0
        self.assertEqual(lengths.mean(), 300)
        now[0] = 17.0
        self.assertIsNone(lengths.mean())


class EngineSeatTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.router = create_app(fleet(Path(folder.name))).state.router
        self.profiles = self.router.profiles

    def test_decode_seats_take_the_lower_of_the_attested_and_profiled_limits(self):
        profiled = self.profiles.get("e3").decode_max_requests
        self.assertEqual(decode_seats(self.router, "e3"), profiled)
        self.router.attested("e3", launch("--max-num-seqs", str(profiled - 4)))
        self.assertEqual(decode_seats(self.router, "e3"), profiled - 4)
        self.router.attested("e3", launch("--max-num-seqs", str(profiled + 4)))
        self.assertEqual(decode_seats(self.router, "e3"), profiled)
        self.router.attested("e3", launch())
        self.assertNotIn("e3", self.router.sequence_limits)
        self.assertEqual(decode_seats(self.router, "e3"), profiled)
        self.assertEqual(decode_seats(self.router, "unprofiled"), 0)

    def test_prefill_seats_fit_back_to_back_prefills_within_the_ttft_target(self):
        self.assertEqual(prefill_seats(self.router, "e0"), 0)
        self.router.input_lengths.add(1_000)
        self.router.input_lengths.add(3_000)
        prefill_s = self.profiles.get("e0").prefill_time(2_000)
        expected = max(1, math.floor(self.router.cfg.slo.ttft_s / prefill_s))
        self.assertEqual(prefill_seats(self.router, "e0"), expected)
        self.router.cfg.slo = replace(self.router.cfg.slo, ttft_s=prefill_s / 2)
        self.assertEqual(prefill_seats(self.router, "e0"), 1)

    def test_the_decode_check_reads_each_engines_seats(self):
        scheduler = self.router.scheduler
        for index in range(2):
            scheduler.monitor.dispatched(
                "e3", Request(f"d{index}", 10, phase=Phase.DECODE, wanted_len=2_000)
            )
        request = Request("new", 10, wanted_len=1)
        budget = scheduler.slo.ttft_s
        self.assertTrue(decode_admits(scheduler, request, ttft_s=budget))
        self.assertEqual(decode_occupancy(scheduler, 10, seats={"e3": 2}).slots, 2)
        self.assertFalse(decode_admits(scheduler, request, ttft_s=budget, seats={"e3": 2}))
        self.assertTrue(decode_admits(scheduler, request, ttft_s=budget, seats={"e3": 0}))

    def test_queueing_needs_only_its_capacity_and_wait(self):
        ServingPolicy(queue_capacity=4, queue_timeout_s=1.0).validate()
        with self.assertRaisesRegex(ValueError, "queue_timeout_s"):
            ServingPolicy(queue_capacity=4).validate()


class SeatHttpTests(HttpHarness):
    async def test_state_and_metrics_export_each_engines_seats(self):
        client = self.client()
        self.router.attested("e3", launch("--max-num-seqs", "5"))
        self.assertEqual((await self.post(client)).status_code, 200)
        state = (await client.get("/narwhal/state")).json()["seats"]
        self.assertIsNotNone(state["mean_input_len"])
        self.assertEqual(state["engines"]["e3"]["decode"], 5)
        self.assertEqual(state["engines"]["e3"]["sequence_limit"], 5)
        prefill = state["engines"]["e0"]["prefill"]
        self.assertGreaterEqual(prefill, 1)
        text = (await client.get("/metrics")).text
        self.assertIn('narwhal_engine_seats{iid="e3",phase="decode"} 5', text)
        self.assertIn(f'narwhal_engine_seats{{iid="e0",phase="prefill"}} {prefill}', text)

    async def test_a_queued_request_waits_for_a_free_prefill_seat(self):
        self.cfg.serving = ServingPolicy(queue_capacity=2, queue_timeout_s=0.05)
        # The original deadline bounds a prefill-seat wait.
        self.cfg.request_timeout_s = 0.2
        client = self.client()
        router = self.router
        self.assertEqual((await self.post(client)).status_code, 200)
        prefill = next(i for i in router.monitor.instances.values() if i.role is Role.PREFILL)
        held = [f"held{index}" for index in range(prefill_seats(router, prefill.iid))]
        for rid in held:
            router.monitor.dispatched(prefill.iid, Request(rid, 5))
        calls = len(self.calls)
        self.assertEqual((await self.post(client)).status_code, 504)
        self.assertEqual(len(self.calls), calls)
        router.monitor.finished(prefill.iid, held[0])
        self.assertEqual((await self.post(client)).status_code, 200)
        for rid in held[1:]:
            router.monitor.finished(prefill.iid, rid)
        self.assert_released()
