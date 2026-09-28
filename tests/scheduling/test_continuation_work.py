"""Price replay work without treating it as another offer or a TTFT trigger."""

import tempfile
import unittest
from pathlib import Path

from narwhal.profiling.store import ProfileStore
from narwhal.scheduling.control import SLO
from narwhal.scheduling.controller import ReactiveController
from narwhal.scheduling.demand import Demand
from narwhal.scheduling.monitor import InstanceMonitor
from narwhal.scheduling.scheduler import GlobalScheduler
from narwhal.types import Instance, Phase, Request, Role
from tests.fixtures import profile


class ContinuationWorkTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.now = 100.0
        self.profiles = ProfileStore(Path(folder.name) / "profiles.json")
        self.monitor = InstanceMonitor(clock=lambda: self.now, profiles=self.profiles)
        for index, role in enumerate((Role.PREFILL, Role.DECODE, Role.DECODE)):
            iid = f"e{index}"
            self.profiles.put(profile(iid))
            self.monitor.add(Instance(iid, f"http://{iid}.invalid", role))
        self.scheduler = GlobalScheduler(
            self.monitor, self.profiles, SLO(1.0, 0.1), clock=lambda: self.now
        )
        self.controller = ReactiveController(
            self.monitor,
            self.scheduler,
            clock=lambda: self.now,
            window_s=60,
            confirmations=2,
            utilization=0.8,
            min_arrivals=10,
            demand_floor=0.1,
        )
        self.scorer = self.controller.scorer
        self.replay = Request(
            "original", 1000, wanted_len=10, arrived_at=0.0, recovery_deadline=110.0
        )

    def test_replay_cannot_start_or_rearm_a_ttft_risk_event(self):
        self.monitor.waiting[self.replay.rid] = self.replay
        self.assertIsNone(self.scorer.project_prefill(self.now, self.replay))
        self.assertIsNone(self.scorer.project_prefill(self.now))
        self.assertFalse(self.controller.note_prefill_risk(self.replay))
        self.assertFalse(self.controller._rearm_prefill_risk(self.now))
        self.assertFalse(self.controller.prefill_risk_pending)
        self.assertEqual(self.replay.arrived_at, 0.0)

    def test_replay_queue_cost_still_contributes_to_an_original_ttft_projection(self):
        self.monitor.waiting[self.replay.rid] = self.replay
        original = Request("next", 100, wanted_len=10, arrived_at=99.75)
        self.monitor.waiting[original.rid] = original
        projection = self.scorer.project_prefill(self.now)
        self.assertIsNotNone(projection)
        self.assertEqual(projection.rid, original.rid)
        self.assertAlmostEqual(projection.projected_ttft_s, 1.37)
        self.assertAlmostEqual(projection.queued_prefill_s, 1.12)
        self.assertEqual(projection.waiting_prefill, 2)
        self.assertEqual(projection.ttft_slo_s, 1.0)
        self.assertTrue(self.controller.note_prefill_risk(original))
        self.monitor.waiting.pop(original.rid)
        self.assertIsNone(self.controller.consume_prefill_risk(self.now))
        self.assertFalse(self.controller.prefill_risk_pending)

    def test_later_replay_does_not_delay_an_ordinary_request_already_waiting(self):
        original = Request("earlier-waiter", 100, wanted_len=10, arrived_at=99.75)
        self.monitor.waiting[original.rid] = original
        self.monitor.waiting[self.replay.rid] = self.replay
        projection = self.scorer.project_prefill(self.now)
        self.assertEqual(projection.rid, original.rid)
        self.assertAlmostEqual(projection.projected_ttft_s, 0.36)
        self.assertAlmostEqual(projection.queued_prefill_s, 1.12)
        self.assertEqual(projection.waiting_prefill, 2)
        self.assertFalse(projection.breached)
        self.assertFalse(self.controller.note_prefill_risk(original))
        self.assertFalse(self.controller._rearm_prefill_risk(self.now))
        self.assertEqual(self.replay.arrived_at, 0.0)
        self.assertEqual(self.replay.recovery_deadline, 110.0)

    def test_ordinary_only_projection_retains_ingress_order(self):
        newer = Request("newer", 100, wanted_len=10, arrived_at=99.8)
        older = Request("older", 1000, wanted_len=10, arrived_at=99.5)
        self.monitor.waiting[newer.rid] = newer
        self.monitor.waiting[older.rid] = older
        projection = self.scorer.project_prefill(self.now, newer)
        self.assertEqual(projection.rid, newer.rid)
        self.assertAlmostEqual(projection.projected_ttft_s, 1.32)
        self.assertAlmostEqual(projection.queued_prefill_s, 1.12)
        self.assertEqual(list(self.monitor.waiting), [newer.rid, older.rid])

    def test_waiting_replay_keeps_ingress_order_for_ordinary_requests(self):
        self.monitor.waiting[self.replay.rid] = self.replay
        newer = Request("newer", 100, wanted_len=10, arrived_at=99.8)
        retried = Request("retried", 1000, wanted_len=10, arrived_at=99.5)
        self.monitor.waiting[newer.rid] = newer
        self.monitor.waiting[retried.rid] = retried
        projection = self.scorer.project_prefill(self.now, newer)
        self.assertEqual(projection.rid, newer.rid)
        self.assertAlmostEqual(projection.projected_ttft_s, 2.33)
        self.assertAlmostEqual(projection.queued_prefill_s, 2.13)
        self.assertEqual(projection.waiting_prefill, 3)

    def test_replay_follows_ordinary_work_published_before_it(self):
        earlier = Request("earlier", 1000, wanted_len=10, arrived_at=99.5)
        self.monitor.waiting[earlier.rid] = earlier
        self.monitor.waiting[self.replay.rid] = self.replay
        later = Request("later", 100, wanted_len=10, arrived_at=99.8)
        self.monitor.waiting[later.rid] = later
        projection = self.scorer.project_prefill(self.now, earlier)
        self.assertEqual(projection.rid, earlier.rid)
        self.assertAlmostEqual(projection.projected_ttft_s, 1.51)

    def test_replay_retains_prefill_and_remaining_decode_load_in_split_projection(self):
        self.monitor.waiting[self.replay.rid] = self.replay
        queued = self.capture()
        self.assertAlmostEqual(queued.queued_prefill_s, 1.01)
        self.assertAlmostEqual(queued.resident_prefill_s, 1.01)
        self.assertEqual(queued.pending_decode_shapes, ((1000, 10),))
        self.assertEqual(queued.pending_decode_tokens, 1005)
        self.assertEqual(queued.pending_decode_requests, 1)

        self.monitor.dispatched("e0", self.replay)
        prefill = self.capture()
        self.assertEqual(prefill.queued_prefill_s, 0)
        self.assertAlmostEqual(prefill.resident_prefill_s, 1.01)
        self.assertEqual(prefill.pending_decode_shapes, ((1000, 10),))

        self.monitor.first_token("e0", self.replay.rid)
        self.replay.phase = Phase.DECODE
        self.monitor.dispatched("e1", self.replay)
        self.monitor.output_token("e1", self.replay.rid)
        decode = self.capture()
        self.assertEqual(decode.resident_decode_tokens, 1001)
        self.assertEqual(decode.resident_decode_requests, 1)
        self.assertEqual(decode.pending_decode_requests, 0)
        self.assertEqual(self.controller.demand.arrival_count(), 0)
        self.assertEqual(self.controller.demand.observed_decode.count(), 0)

    def test_original_request_projection_keeps_elapsed_ingress_time(self):
        original = Request("original", 100, wanted_len=10, arrived_at=90.0)
        self.monitor.waiting[original.rid] = original
        projection = self.scorer.project_prefill(self.now, original)
        self.assertAlmostEqual(projection.projected_ttft_s, 10.11)
        self.assertEqual(projection.ttft_slo_s, 1.0)
        self.assertTrue(projection.breached)

    def test_excluded_prefill_pool_does_not_price_busy_decode_fallback_as_isolated(self):
        busy = Request("resident", 100, phase=Phase.DECODE, wanted_len=20)
        self.monitor.dispatched("e1", busy)
        survivor = self.scheduler.schedule(self.replay, exclude={"e0", "e2"})
        self.assertEqual(survivor.iid, "e1")
        self.assertTrue(self.scheduler.live_instances(Role.PREFILL))
        self.assertEqual(
            self.scheduler.prefill_admission_price(self.replay, survivor), float("inf")
        )

    def capture(self):
        return self.scorer.capture(
            self.now, Demand(0.0, 0.0, 0, 0), utilization=0.8, observed_load=(0.0, 0.0)
        )
