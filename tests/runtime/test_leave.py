"""Remove an engine from the fleet once its drain completes."""

import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from narwhal.config import EngineSpec
from narwhal.config.model import SharedDeviceAllocation
from narwhal.runtime.lifecycle import leave
from narwhal.runtime.lifecycle.leave import begin_leave, complete_leaves
from narwhal.runtime.lifecycle.records import LifecycleError, ValidationOutcome
from narwhal.runtime.monitoring import monitor_once
from narwhal.serving.app import create_app
from narwhal.serving.router.membership import Membership
from narwhal.types import Phase, Request, Role
from tests.fixtures import fleet


class LeaveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.cfg = fleet(Path(folder.name), engines=("e0", "e1", "e3"))
        self.router = create_app(self.cfg).state.router
        self.addAsyncCleanup(self.router.engines.aclose)
        self.manager = self.router.lifecycle
        self.capture = AsyncMock(side_effect=self.identities)
        patcher = patch.object(leave, "capture_process_identities", self.capture)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def identities(self, cfg, engines, *, specs, transport=None):
        return dict.fromkeys(engines, 100.0), {}

    async def leave(self, iid="e1", deadline_s=60.0):
        async with self.manager.lock:
            return await begin_leave(self.router, iid, deadline_s=deadline_s)

    def actions(self):
        return [(e["action"], e.get("iid"), e.get("generation")) for e in self.manager.events]

    async def test_an_idle_engine_leaves_at_once(self):
        record = await self.leave()
        self.assertTrue(record.leaving)
        self.assertNotIn("e1", self.router.membership)
        self.assertEqual(self.router.membership.generation, 1)
        self.assertNotIn("e1", self.manager.records)
        self.assertIn(("leave_started", "e1", 0), self.actions())
        self.assertIn(("left", "e1", 1), self.actions())
        self.assertEqual(self.capture.await_args.kwargs["specs"]["e1"].iid, "e1")

    async def test_resident_work_finishes_before_the_engine_leaves(self):
        monitor = self.router.monitor
        request = Request("r", 64)
        monitor.dispatched("e1", request)
        await self.leave()
        self.assertIn("e1", self.router.membership)
        self.assertEqual(self.manager.records["e1"].state, "draining")
        self.assertNotIn("e1", {i.iid for i in self.router.scheduler.live_instances()})
        # The prefill completes and its decode leg lands on another engine.
        monitor.first_token("e1", "r")
        request.phase = Phase.DECODE
        monitor.dispatched("e3", request)
        self.assertEqual(complete_leaves(self.router), [])
        # The decode leg still pulls the KV from the leaving producer until its first token.
        self.assertEqual(monitor.pending_handoffs("e1"), 1)
        monitor.output_token("e3", "r")
        self.assertEqual(complete_leaves(self.router), ["e1"])
        self.assertNotIn("e1", self.router.membership)
        # The request keeps decoding on its engine and finishes normally.
        monitor.output_token("e3", "r")
        monitor.finished("e3", "r")
        self.assertEqual(self.router.monitor.instances["e3"].decode, {})

    async def test_the_monitoring_pass_removes_a_drained_engine(self):
        self.router.monitor.dispatched("e1", Request("r", 64))
        await self.leave()
        self.router.monitor.finished("e1", "r")
        with patch("narwhal.runtime.monitoring.readmit", AsyncMock(return_value=[])):
            await monitor_once(self.router)
        self.assertNotIn("e1", self.router.membership)
        self.assertIn(("left", "e1", 1), self.actions())

    async def test_a_passed_deadline_forces_nothing(self):
        self.router.monitor.dispatched("e1", Request("r", 64))
        await self.leave(deadline_s=0.001)
        time.sleep(0.01)
        self.assertEqual(complete_leaves(self.router), [])
        self.assertEqual(self.manager.records["e1"].state, "deadline_exceeded")
        self.assertIn("e1", self.router.membership)
        self.assertIn("r", self.router.monitor.instances["e1"].prefill)
        self.router.monitor.finished("e1", "r")
        self.assertEqual(complete_leaves(self.router), ["e1"])

    async def test_ejection_does_not_change_membership(self):
        self.router.scheduler.eject("e1", "liveness")
        self.assertEqual(complete_leaves(self.router), [])
        self.assertIn("e1", self.router.membership)
        self.assertEqual(self.router.membership.generation, 0)
        # An ejected engine can still leave; its last verified identity binds the drain.
        self.manager.process_starts["e1"] = 90.0
        self.capture.side_effect = None
        self.capture.return_value = ({}, {"e1": "process identity unreadable: ConnectError"})
        await self.leave()
        self.assertNotIn("e1", self.router.membership)

    async def test_a_failed_identity_capture_holds_the_leave_until_a_retry(self):
        self.capture.side_effect = None
        self.capture.return_value = ({}, {"e1": "process identity unreadable: ConnectError"})
        record = await self.leave()
        self.assertEqual(record.state, "blocked")
        self.assertIn("e1", self.router.membership)
        self.capture.side_effect = self.identities
        await self.leave()
        self.assertNotIn("e1", self.router.membership)

    async def test_readmission_cancels_a_leave(self):
        self.router.monitor.dispatched("e1", Request("r", 64))
        await self.leave()
        self.router.monitor.finished("e1", "r")
        self.manager.refresh()
        self.manager.mark_validating(["e1"])
        outcome = ValidationOutcome(starts={"e1": 100.0})
        self.manager.readmitted(["e1"], outcome)
        self.assertFalse(self.manager.records["e1"].leaving)
        self.assertEqual(complete_leaves(self.router), [])
        self.assertIn("e1", self.router.membership)

    async def test_refusals(self):
        with self.assertRaisesRegex(LifecycleError, "unknown engine: e9"):
            await self.leave("e9")
        self.router.scheduler.eject("e0", "liveness")
        self.router.scheduler.eject("e3", "liveness")
        with self.assertRaisesRegex(LifecycleError, "last schedulable engine"):
            await self.leave()
        self.router.scheduler.record_answer("e0", "health")
        self.router.scheduler.record_answer("e3", "health")
        self.router.scheduler.min_decode = 2
        with self.assertRaisesRegex(LifecycleError, "fewer than min_prefill \\+ min_decode = 3"):
            await self.leave()
        self.router.scheduler.min_decode = 1
        self.cfg.engine_restart_policy = "whole_wave"
        with self.assertRaisesRegex(LifecycleError, "whole_wave"):
            await self.leave()
        self.cfg.engine_restart_policy = "individual"
        contract = self.cfg.engine_contract
        self.cfg.engine_contract = None
        with self.assertRaisesRegex(LifecycleError, "complete engine_contract"):
            await self.leave()
        self.cfg.engine_contract = contract
        allocation = SharedDeviceAllocation("g", "GPU-x", 0.5, 0.4)
        shared = tuple(
            replace(spec, shared_device=allocation) if spec.iid == "e1" else spec
            for spec in self.router.membership
        )
        self.router.membership = Membership(0, shared)
        with self.assertRaisesRegex(LifecycleError, "shared-device group"):
            await self.leave()
        self.capture.assert_not_awaited()
        self.assertEqual(self.manager.records, {})

    async def test_a_whole_wave_drain_covers_the_current_snapshot(self):
        self.router.apply_membership(
            self.router.membership.joined(EngineSpec("e6", "http://e6:8000", role=Role.DECODE))
        )
        with self.assertRaisesRegex(LifecycleError, "every configured engine"):
            self.manager.begin(["e0", "e1", "e3"], wave=True, deadline_s=60.0)
        self.manager.begin(sorted(self.router.membership.ids), wave=True, deadline_s=60.0)
        self.assertEqual(sorted(self.manager.records), ["e0", "e1", "e3", "e6"])
        # A leave waits for the wave to finish.
        with self.assertRaisesRegex(LifecycleError, "lifecycle action already active"):
            await self.leave()


if __name__ == "__main__":
    unittest.main()
