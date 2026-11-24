"""Reset reactive role control when the router's engine set changes."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace

import httpx

from narwhal.config import EngineSpec
from narwhal.scheduling.reactive import Departure, SwitchDrain
from narwhal.types import Role
from tests.scheduling.test_reactive import Fleet


class MembershipControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        transport = httpx.MockTransport(lambda request: httpx.Response(500))
        self.fleet = Fleet(directory.name, transport=transport)
        self.addCleanup(self.fleet.telemetry.stop)
        self.addCleanup(self.fleet.journal.close)
        self.router = self.fleet.router
        self.profiles = self.fleet.profiles

    def join(self, iid: str = "e6", role: Role = Role.DECODE) -> None:
        self.profiles.put(replace(self.profiles.get("e1"), iid=iid))
        spec = EngineSpec(iid, f"http://{iid}", role)
        self.router.apply_membership(self.router.membership.joined(spec))

    def test_a_join_interrupts_the_pending_proposal(self) -> None:
        fleet = self.fleet
        self.assertIsNone(fleet.step())
        policy = fleet.controller.reactive
        self.assertEqual(policy.proposal[0], 2)
        self.assertEqual(policy.confirmations, 1)
        self.join()
        self.assertIsNone(policy.proposal)
        self.assertEqual(policy.confirmations, 0)
        # The next pass scores splits of the seven engines and waits for confirmation again.
        self.assertIsNone(fleet.step())
        decision = fleet.scheduler.roles._last_decision
        self.assertEqual(decision["prefill"] + decision["decode"], 7)
        self.assertEqual(decision["result"], "held")
        self.assertEqual(policy.confirmations, 1)
        self.assertEqual(fleet.scheduler.roles.flips, [])

    def test_a_leave_interrupts_the_pending_proposal(self) -> None:
        fleet = self.fleet
        self.assertIsNone(fleet.step())
        self.router.apply_membership(self.router.membership.left("e5"))
        self.assertIsNone(fleet.controller.reactive.proposal)
        self.assertIsNone(fleet.step())
        decision = fleet.scheduler.roles._last_decision
        self.assertEqual(decision["prefill"] + decision["decode"], 5)
        self.assertEqual(fleet.scheduler.roles.flips, [])

    def test_runs_drains_and_urgent_events_reset(self) -> None:
        fleet = self.fleet
        policy = fleet.controller.reactive
        policy.settled_since = policy.balanced_since = 1.0
        policy.unsettled_since = policy.lead_break_since = 1.0
        policy.departure = Departure(1, 1.0)
        policy.drain = SwitchDrain(1, "e2", 1.0, "rule", 1)
        fleet.scheduler.switch_holds["e2"] = fleet.now
        fleet.scheduler.switch_holds["e3"] = fleet.now
        fleet.scheduler.roles._panic_sustained = 3
        fleet.scheduler.roles.held_donor = "e3"
        fleet.controller._prefill_urgency = object()  # type: ignore[assignment]
        fleet.controller._decode_urgency = object()  # type: ignore[assignment]
        self.join()
        self.assertIsNone(policy.drain)
        self.assertIsNone(policy.departure)
        self.assertEqual(
            (
                policy.settled_since,
                policy.balanced_since,
                policy.unsettled_since,
                policy.lead_break_since,
            ),
            (None, None, None, None),
        )
        self.assertEqual(fleet.scheduler.switch_holds, {})
        self.assertEqual(fleet.scheduler.roles._panic_sustained, 0)
        self.assertIsNone(fleet.scheduler.roles.held_donor)
        self.assertFalse(fleet.controller.prefill_risk_pending)
        self.assertIsNone(fleet.controller._decode_urgency)

    def test_floors_count_the_new_engine_set(self) -> None:
        controller = self.fleet.controller
        scheduler = self.fleet.scheduler
        scheduler.min_decode = 2
        self.assertTrue(controller.within_floors(4))
        self.assertFalse(controller.within_floors(5))
        self.join()
        self.assertTrue(controller.within_floors(5))
        self.assertEqual(controller._clamp_prefill_target(9), 5)
        for iid in ("e6", "e5"):
            self.router.apply_membership(self.router.membership.left(iid))
        self.assertFalse(controller.within_floors(4))
        self.assertEqual(controller._clamp_prefill_target(9), 3)

    def test_a_joined_engine_dwells_from_its_join_time(self) -> None:
        fleet = self.fleet
        roles = fleet.scheduler.roles
        fleet.now += 100.0
        # The only prefill engine has dwelt long enough; a joined prefill engine has not.
        self.assertFalse(roles.decode_move_waits())
        joined_at = fleet.now
        self.join("e6", Role.PREFILL)
        self.assertEqual(roles._last_flip["e6"], joined_at)
        fleet.scheduler.pinned = frozenset({"e0"})
        roles._last_p2d_flip = -1e9
        self.assertTrue(roles.decode_move_waits())
        fleet.now += fleet.scheduler.th.dwell_s
        self.assertFalse(roles.decode_move_waits())


if __name__ == "__main__":
    unittest.main()
