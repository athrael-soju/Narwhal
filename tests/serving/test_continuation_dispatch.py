"""Keep recovery work and exclusions intact through phase-capacity waits."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from narwhal.profiling.store import ProfileStore
from narwhal.scheduling.control import SLO
from narwhal.scheduling.demand import DemandModel
from narwhal.scheduling.monitor import InstanceMonitor
from narwhal.scheduling.scheduler import GlobalScheduler
from narwhal.serving.admission import QueueExpired
from narwhal.serving.dispatch import Dispatcher
from narwhal.serving.policy import ServingPolicy
from narwhal.types import Instance, Phase, Request, Role
from tests.fixtures import profile


class ContinuationDispatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.now = 10.0
        self.profiles = ProfileStore(Path(folder.name) / "profiles.json")
        self.monitor = InstanceMonitor(clock=lambda: self.now, profiles=self.profiles)
        for index, role in enumerate((Role.PREFILL, Role.PREFILL, Role.DECODE)):
            iid = f"e{index}"
            self.profiles.put(profile(iid))
            self.monitor.add(Instance(iid, f"http://{iid}.invalid", role))
        self.scheduler = GlobalScheduler(
            self.monitor, self.profiles, SLO(1.0, 0.1), clock=lambda: self.now
        )
        self.router = SimpleNamespace(
            _clock=lambda: self.now,
            max_concurrent=4,
            cfg=SimpleNamespace(
                request_timeout_s=30.0,
                serving=ServingPolicy(prefill_concurrency=1, decode_concurrency=1),
            ),
            monitor=self.monitor,
            scheduler=self.scheduler,
            lifecycle_blocked="",
            monitoring_degraded=False,
            failover_blocked="",
            standby=False,
            lease=None,
        )
        self.dispatcher = Dispatcher(self.router)
        self.monitor.on_capacity_change = self.dispatcher.notify
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def enqueue(self, request, *, excluded=frozenset(), deadline=30.0):
        task = asyncio.create_task(
            self.dispatcher.place(request, deadline=deadline, excluded=excluded)
        )
        self.tasks.append(task)
        await asyncio.sleep(0)
        return task

    async def test_exclusion_precedes_role_fallback_in_both_phases(self):
        for phase, excluded, expected in (
            (Phase.PREFILL, frozenset({"e0", "e1"}), "e2"),
            (Phase.DECODE, frozenset({"e2"}), "e0"),
        ):
            with self.subTest(phase=phase):
                replay = Request("original", 120, phase=phase, wanted_len=8)
                chosen = await self.dispatcher.place(replay, deadline=30, excluded=excluded)
                self.assertEqual(chosen.iid, expected)
                self.assertFalse(self.monitor.waiting)
                self.assertEqual(len(self.dispatcher.queues[phase]), 0)

    async def test_queue_rechecks_current_roles_and_capacity_with_exclusion(self):
        occupied = Request("resident", 20)
        self.monitor.dispatched("e1", occupied)
        replay = Request("original", 120, wanted_len=8)
        task = await self.enqueue(replay, excluded=frozenset({"e0"}))
        self.assertFalse(task.done())
        self.assertIs(self.monitor.waiting[replay.rid], replay)
        self.assertEqual(len(self.dispatcher.queues[Phase.PREFILL]), 1)

        # The failed engine remains excluded even after its global hold expires.
        self.scheduler.quarantined["e0"] = self.now
        self.monitor.instances["e1"].role = Role.DECODE
        self.monitor.instances["e2"].role = Role.PREFILL
        self.dispatcher.notify()
        chosen = await asyncio.wait_for(task, 1)
        self.assertEqual(chosen.iid, "e2")
        self.assertIn(occupied.rid, self.monitor.instances["e1"].prefill)
        self.assertFalse(self.monitor.waiting)

    async def test_queue_rechecks_survivor_health_and_lifecycle_holdouts(self):
        for holdout in ("draining", "ejected", "quarantined"):
            with self.subTest(holdout=holdout):
                resident = Request("resident", 20)
                self.monitor.dispatched("e1", resident)
                replay = Request("original", 120, wanted_len=8)
                task = await self.enqueue(replay, excluded=frozenset({"e0"}))
                self.assertFalse(task.done())
                held = getattr(self.scheduler, holdout)
                if isinstance(held, set):
                    held.add("e1")
                else:
                    held["e1"] = self.now + 10
                self.monitor.finished("e1", resident.rid)
                self.assertEqual((await asyncio.wait_for(task, 1)).iid, "e2")
                if isinstance(held, set):
                    held.remove("e1")
                else:
                    del held["e1"]

    async def test_global_prefill_fences_still_hold_recovery_work(self):
        for field, value in (
            ("lifecycle_blocked", "whole-wave maintenance"),
            ("monitoring_degraded", True),
            ("failover_blocked", "lease renewal failed"),
            ("standby", True),
        ):
            with self.subTest(field=field):
                setattr(self.router, field, value)
                replay = Request("original", 120, wanted_len=8)
                task = await self.enqueue(replay, excluded=frozenset({"e0"}))
                self.assertFalse(task.done())
                setattr(self.router, field, False if type(value) is bool else "")
                self.dispatcher.notify()
                self.assertEqual((await asyncio.wait_for(task, 1)).iid, "e1")

    async def test_cancelled_recovery_releases_waiting_work_and_phase_queue(self):
        replay = Request("original", 120, wanted_len=8)
        task = await self.enqueue(replay, excluded=frozenset(self.monitor.instances))
        self.assertIs(self.monitor.waiting[replay.rid], replay)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.monitor.waiting)
        self.assertEqual(len(self.dispatcher.queues[Phase.PREFILL]), 0)
        self.assertTrue(all(not inst.prefill for inst in self.monitor.instances.values()))

    async def test_decode_recovery_rechecks_control_fences_after_capacity_wakes(self):
        for field, value, cleared in (
            ("failover_blocked", "lease renewal failed", ""),
            ("standby", True, False),
            ("lease", SimpleNamespace(valid=lambda: False), None),
        ):
            with self.subTest(field=field):
                resident = Request("resident", 20, phase=Phase.DECODE)
                self.monitor.dispatched("e2", resident)
                replay = Request(
                    "original", 120, phase=Phase.DECODE, wanted_len=8, recovery_deadline=30.0
                )
                task = await self.enqueue(replay, excluded=frozenset({"e0"}))
                self.assertFalse(task.done())
                setattr(self.router, field, value)
                self.monitor.finished("e2", resident.rid)
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                self.assertIs(self.monitor.waiting[replay.rid], replay)
                setattr(self.router, field, cleared)
                self.dispatcher.notify()
                self.assertEqual((await asyncio.wait_for(task, 1)).iid, "e2")

    async def test_ordinary_decode_keeps_existing_control_handoff_behaviour(self):
        self.router.standby = True
        ordinary = Request("ordinary", 100, phase=Phase.DECODE, wanted_len=8)
        chosen = await self.dispatcher.place(ordinary, deadline=30.0)
        self.assertEqual(chosen.iid, "e2")

    async def test_recovery_wait_uses_the_original_deadline(self):
        replay = Request("original", 120, wanted_len=8, arrived_at=0.0)
        task = await self.enqueue(replay, excluded=frozenset(self.monitor.instances), deadline=11.0)
        self.now = 11.0
        self.dispatcher.notify()
        with self.assertRaises(QueueExpired):
            await task
        self.assertFalse(self.monitor.waiting)
        self.assertEqual(len(self.dispatcher.queues[Phase.PREFILL]), 0)

    async def test_augmented_work_prices_residency_without_another_offer(self):
        original = Request("original", 100, wanted_len=28, output_len=20, arrived_at=0.0)
        replay = Request(original.rid, 120, wanted_len=8, arrived_at=original.arrived_at)
        demand = DemandModel(
            self.monitor, self.scheduler, lambda: self.now, window_s=60.0, bucket_s=1.0
        )
        demand.saw_arrival(original.input_len, wanted_len=original.wanted_len, at=0.0)
        expected = demand.expected_decode.summary()
        chosen = await self.dispatcher.place(replay, deadline=30, excluded=frozenset({"e0"}))
        self.assertEqual(self.scheduler.cost(replay, chosen)[1], 0.13)
        self.monitor.dispatched(chosen.iid, replay)
        self.assertEqual(chosen.prefill_tokens(), 120)
        self.assertIs(chosen.prefill[original.rid], replay)
        self.monitor.first_token(chosen.iid, replay.rid)
        replay.phase = Phase.DECODE
        decode = self.scheduler.schedule(replay, exclude={"e0"})
        self.monitor.dispatched(decode.iid, replay)
        self.monitor.output_token(decode.iid, replay.rid)
        self.assertEqual(decode.decode_tokens(), 121)
        self.assertEqual(
            (original.input_len, original.wanted_len, original.output_len), (100, 28, 20)
        )
        self.assertEqual(demand.arrival_count(), 1)
        self.assertEqual(demand.expected_decode.summary(), expected)
        self.assertEqual(demand.observed_decode.count(), 0)
        self.monitor.finished(decode.iid, replay.rid)

    async def test_queue_free_scheduler_excludes_failed_prefill_affinity(self):
        replay = Request("original", 120, phase=Phase.DECODE, prefill_instance="e2", wanted_len=8)
        chosen = self.scheduler.schedule(replay, exclude={"e2"})
        self.assertEqual(chosen.iid, "e0")
        self.assertEqual(replay.prefill_instance, "e2")
