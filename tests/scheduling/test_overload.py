"""Check pinned-role placement bounds and decode-capacity admission."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from narwhal.serving.app import create_app
from narwhal.types import Phase, Request
from tests.fixtures import fleet


class PinnedPlacementTests(unittest.TestCase):
    def router(self, *, pinned):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        cfg = fleet(Path(folder.name))
        if pinned:
            cfg.engines = [replace(spec, pin=True) for spec in cfg.engines]
        return create_app(cfg).state.router

    def test_pinned_prefill_engines_take_no_decode_legs(self):
        router = self.router(pinned=True)
        router.scheduler.eject("e3")
        with self.assertRaises(RuntimeError):
            router.scheduler.schedule(Request("r", 10, phase=Phase.DECODE))

    def test_unpinned_fleets_fall_back_to_the_other_role(self):
        router = self.router(pinned=False)
        router.scheduler.eject("e3")
        self.assertEqual(router.scheduler.schedule(Request("r", 10, phase=Phase.DECODE)).iid, "e0")


class DecodeAdmissionTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.cfg = fleet(Path(folder.name))
        self.router = create_app(self.cfg).state.router
        self.scheduler = self.router.scheduler
        self.request = Request("new", 10)

    def fill(self, count):
        for index in range(count):
            self.scheduler.monitor.dispatched("e3", Request(f"d{index}", 10, phase=Phase.DECODE))

    def test_decode_admission_follows_measured_concurrency(self):
        self.assertTrue(self.scheduler.decode_admits(self.request))
        limit = self.scheduler.profiles.get("e3").decode_max_requests
        self.fill(limit)
        self.assertFalse(self.scheduler.decode_admits(self.request))

    def test_a_serving_concurrency_limit_takes_precedence(self):
        self.fill(2)
        self.assertFalse(self.scheduler.decode_admits(self.request, concurrency=2))
        self.assertTrue(self.scheduler.decode_admits(self.request, concurrency=3))

    def test_decode_admission_follows_the_tpot_budget(self):
        profile = self.scheduler.profiles.get("e3")
        self.scheduler.profiles.put(replace(profile, tpot_intercept=10 * self.scheduler.slo.tpot_s))
        self.assertFalse(self.scheduler.decode_admits(self.request))

    def test_a_fleet_without_live_decode_engines_admits(self):
        self.fill(self.scheduler.profiles.get("e3").decode_max_requests)
        self.scheduler.eject("e3")
        self.assertTrue(self.scheduler.decode_admits(self.request))
