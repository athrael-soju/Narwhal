"""Check pinned-role placement bounds and decode-capacity admission."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from narwhal.serving.admission import QueueExpired
from narwhal.serving.app import create_app
from narwhal.serving.policy import ServingPolicy
from narwhal.types import Phase, Request, Role
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

    def test_drift_eviction_keeps_the_engine_that_alone_serves_a_pinned_role(self):
        for pinned, ejected in ((True, False), (False, True)):
            with self.subTest(pinned=pinned):
                scheduler = self.router(pinned=pinned).scheduler
                with patch.object(scheduler.health, "tick", return_value=[("evict", "e3")]):
                    scheduler.health_pass()
                self.assertEqual("e3" in scheduler.ejected, ejected)

    def test_unpinned_fleets_fall_back_to_the_other_role(self):
        router = self.router(pinned=False)
        router.scheduler.eject("e3")
        self.assertEqual(router.scheduler.schedule(Request("r", 10, phase=Phase.DECODE)).iid, "e0")

    def test_a_role_is_placeable_through_its_engines_or_unpinned_engines(self):
        for pinned, placeable in ((True, False), (False, True)):
            with self.subTest(pinned=pinned):
                router = self.router(pinned=pinned)
                router.scheduler.eject("e3")
                self.assertIs(router.scheduler.role_placeable(Role.DECODE), placeable)
                self.assertTrue(router.scheduler.role_placeable(Role.PREFILL))


class DispatcherFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_dispatcher_falls_back_only_to_unpinned_engines(self):
        for pinned in (True, False):
            with self.subTest(pinned=pinned):
                folder = tempfile.TemporaryDirectory()
                self.addCleanup(folder.cleanup)
                cfg = fleet(Path(folder.name))
                cfg.serving = ServingPolicy(
                    queue_capacity=4,
                    queue_timeout_s=5.0,
                    prefill_concurrency=4,
                    decode_concurrency=4,
                    handoff_timeout_s=5.0,
                )
                cfg.engines = [replace(spec, pin=pinned) for spec in cfg.engines]
                router = create_app(cfg).state.router
                self.addAsyncCleanup(router.engines.aclose)
                router.scheduler.eject("e3")
                request = Request("r", 10, phase=Phase.DECODE)
                place = router.dispatcher.place(request, deadline=router._clock() + 0.05)
                if pinned:
                    with self.assertRaises(QueueExpired):
                        await place
                else:
                    self.assertEqual((await place).iid, "e0")


class DecodeAdmissionTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.cfg = fleet(Path(folder.name))
        self.router = create_app(self.cfg).state.router
        self.scheduler = self.router.scheduler
        self.request = Request("new", 10)

    def fill(self, count):
        for _ in range(count):
            rid = f"d{len(self.scheduler.monitor.instances['e3'].decode)}"
            self.scheduler.monitor.dispatched("e3", Request(rid, 10, phase=Phase.DECODE))

    def test_decode_admission_follows_measured_concurrency(self):
        self.assertTrue(self.scheduler.decode_admits(self.request))
        limit = self.scheduler.profiles.get("e3").decode_max_requests
        self.fill(limit)
        self.assertFalse(self.scheduler.decode_admits(self.request))

    def prefilling(self, count):
        for index in range(count):
            self.scheduler.monitor.dispatched("e0", Request(f"p{index}", 10))

    def test_a_serving_concurrency_limit_caps_the_measured_limit(self):
        self.fill(2)
        self.assertFalse(self.scheduler.decode_admits(self.request, concurrency=2))
        self.assertTrue(self.scheduler.decode_admits(self.request, concurrency=3))

    def test_requests_in_prefill_hold_no_decode_slot(self):
        limit = self.scheduler.profiles.get("e3").decode_max_requests
        self.fill(limit - 1)
        self.prefilling(limit + 8)
        self.assertTrue(self.scheduler.decode_admits(self.request))

    def test_capacity_follows_the_request_context(self):
        profile = self.scheduler.profiles.get("e3")
        context = profile.decode_max_kv_tokens // 2
        request = Request("long", context - 1, wanted_len=1)
        limit = profile.decode_request_limit(context)
        self.assertLess(limit, profile.decode_max_requests)
        self.fill(limit - 1)
        self.assertTrue(self.scheduler.decode_admits(request))
        self.fill(limit)
        self.assertFalse(self.scheduler.decode_admits(request))

    def test_requests_waiting_for_a_decode_slot_count_as_committed(self):
        self.fill(self.scheduler.profiles.get("e3").decode_max_requests - 1)
        self.assertTrue(self.scheduler.decode_admits(self.request))
        self.scheduler.monitor.waiting["w"] = Request("w", 10, phase=Phase.DECODE)
        self.assertFalse(self.scheduler.decode_admits(self.request))

    def test_decode_admission_follows_the_tpot_budget_under_load(self):
        for index in range(3):
            self.scheduler.monitor.dispatched(
                "e3", Request(f"kv{index}", 30_000, phase=Phase.DECODE)
            )
        self.assertFalse(self.scheduler.decode_admits(Request("new", 20_000)))

    def test_a_request_that_misses_the_budget_alone_is_left_to_placement(self):
        self.assertTrue(self.scheduler.decode_admits(Request("huge", 150_000)))

    def test_an_engine_without_a_profile_admits(self):
        self.fill(self.scheduler.profiles.get("e3").decode_max_requests)
        get = self.scheduler.profiles.get
        with patch.object(
            self.scheduler.profiles,
            "get",
            side_effect=lambda iid, *args, **kwargs: (
                None if iid == "e3" else get(iid, *args, **kwargs)
            ),
        ):
            self.assertTrue(self.scheduler.decode_admits(self.request))

    def test_a_fleet_without_live_decode_engines_admits(self):
        self.fill(self.scheduler.profiles.get("e3").decode_max_requests)
        self.scheduler.eject("e3")
        self.assertTrue(self.scheduler.decode_admits(self.request))
