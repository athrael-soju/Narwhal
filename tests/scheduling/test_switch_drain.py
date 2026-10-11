from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from narwhal.profiling.store import ProfileStore
from narwhal.scheduling.control import SLO, Thresholds
from narwhal.scheduling.monitor import InstanceMonitor
from narwhal.scheduling.scheduler.placement import GlobalScheduler
from narwhal.types import Instance, Phase, Request, Role
from tests.fixtures import profile


class SwitchDrainTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        with tempfile.TemporaryDirectory() as folder:
            profiles = ProfileStore(Path(folder) / "profiles.json")
        monitor = InstanceMonitor(profiles=profiles)
        for iid, role in (("p1", Role.PREFILL), ("p2", Role.PREFILL), ("d1", Role.DECODE)):
            profiles.put(profile(iid))
            monitor.add(Instance(iid, f"http://{iid}", role))
        profiles.put(profile("d2"))
        monitor.add(Instance("d2", "http://d2", Role.DECODE))
        self.scheduler = GlobalScheduler(
            monitor, profiles, SLO(2.0, 0.05), Thresholds(), clock=lambda: self.now
        )
        self.scheduler.availability.roles_bound = True
        self.scheduler.switch_requires_idle = True
        self.decodes = {iid: monitor.instances[iid].decode for iid in ("d1", "d2")}
        self.decodes["d1"].update(r1=Request("r1", 128), r2=Request("r2", 128))
        self.decodes["d2"]["r3"] = Request("r3", 128)

    def test_a_busy_donor_stops_taking_work_and_switches_once_idle(self):
        roles = self.scheduler.roles
        self.assertIsNone(roles.flip(Role.PREFILL, "test"))
        self.assertIn("holding d2 out of new placements", roles._last_decision["reason"])
        self.assertEqual(self.scheduler.held_for_switch(), {"d2"})
        leg = Request("r4", 128, phase=Phase.DECODE)
        self.assertEqual(self.scheduler.schedule(leg).iid, "d1")
        self.decodes["d1"]["r4"] = Request("r4", 128)
        # The held donor stays the donor although another engine is now lighter.
        self.decodes["d1"].pop("r1")
        self.decodes["d1"].pop("r2")
        self.decodes["d2"]["r5"] = Request("r5", 128)
        self.assertIsNone(roles.flip(Role.PREFILL, "test"))
        self.assertEqual(self.scheduler.held_for_switch(), {"d2"})
        self.decodes["d2"].clear()
        moved = roles.flip(Role.PREFILL, "test")
        self.assertIsNotNone(moved)
        self.assertEqual(moved.iid, "d2")
        self.assertEqual(self.scheduler.held_for_switch(), set())

    def test_an_engine_that_goes_idle_switches_and_releases_the_hold(self):
        roles = self.scheduler.roles
        self.assertIsNone(roles.flip(Role.PREFILL, "test"))
        self.decodes["d1"].clear()
        moved = roles.flip(Role.PREFILL, "test")
        self.assertIsNotNone(moved)
        self.assertEqual(moved.iid, "d1")
        self.assertEqual(self.scheduler.held_for_switch(), set())

    def test_a_hold_lapses_without_a_new_request(self):
        self.assertIsNone(self.scheduler.roles.flip(Role.PREFILL, "test"))
        self.now += self.scheduler.switch_hold_s + 1
        self.assertEqual(self.scheduler.held_for_switch(), set())
        leg = Request("r4", 128, phase=Phase.DECODE)
        self.assertIn(self.scheduler.schedule(leg).iid, {"d1", "d2"})


if __name__ == "__main__":
    unittest.main()
