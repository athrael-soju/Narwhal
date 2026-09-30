"""Check pinned-role placement bounds and decode-capacity admission."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from narwhal.config import FleetConfig
from narwhal.profiling.store import ProfileStore
from narwhal.serving.admission import QueueExpired
from narwhal.serving.app import create_app
from narwhal.serving.policy import ServingPolicy
from narwhal.types import Phase, Request, Role
from tests.fixtures import ROOT, fleet, profile


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

    def test_holds_keep_every_role_placeable_through_fallback_engines(self):
        """An unpinned engine covering another role's legs stays while it alone covers them."""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        cfg = FleetConfig.load(ROOT / "tests/data/fleet.json")
        e0, e1, e3 = cfg.engines[0], cfg.engines[1], cfg.engines[3]
        cfg.engines = [e0, replace(e1, pin=True), replace(e3, pin=True)]
        cfg.profiles_path = Path(folder.name) / "profiles.json"
        store = ProfileStore(cfg.profiles_path)
        for spec in cfg.engines:
            store.put(profile(spec.iid))
        scheduler = create_app(cfg).state.router.scheduler
        self.assertTrue(scheduler.quarantine("e3", 30.0))
        self.assertEqual(scheduler.schedule(Request("a", 10, phase=Phase.DECODE)).iid, "e0")
        self.assertFalse(scheduler.quarantine("e0", 30.0))
        self.assertTrue(scheduler.role_placeable(Role.DECODE))
        self.assertTrue(scheduler.role_placeable(Role.PREFILL))

    def test_removing_a_covering_engine_releases_holds_that_lose_coverage(self):
        """An ejection or drain returns held engines whose roles lost their other engines."""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for remove in ("eject", "drain"):
            with self.subTest(remove=remove):
                cfg = FleetConfig.load(ROOT / "tests/data/fleet.json")
                cfg.engines = [
                    replace(spec, pin=True)
                    for spec in cfg.engines
                    if spec.iid in ("e0", "e3", "e4", "e5")
                ]
                cfg.profiles_path = Path(folder.name) / f"profiles-{remove}.json"
                store = ProfileStore(cfg.profiles_path)
                for spec in cfg.engines:
                    store.put(profile(spec.iid))
                scheduler = create_app(cfg).state.router.scheduler
                self.assertTrue(scheduler.quarantine("e3", 30.0))
                getattr(scheduler, remove)("e4")
                self.assertIn("e3", scheduler.quarantined)
                getattr(scheduler, remove)("e5")
                self.assertNotIn("e3", scheduler.quarantined)
                self.assertTrue(scheduler.role_placeable(Role.DECODE))

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

    def fill(self, count, wanted_len=0):
        for _ in range(count):
            rid = f"d{len(self.scheduler.monitor.instances['e3'].decode)}"
            self.scheduler.monitor.dispatched(
                "e3", Request(rid, 10, phase=Phase.DECODE, wanted_len=wanted_len)
            )

    def test_decode_admission_follows_measured_concurrency(self):
        self.assertTrue(self.scheduler.decode_admits(self.request))
        limit = self.scheduler.profiles.get("e3").decode_max_requests
        self.fill(limit)
        self.assertFalse(self.scheduler.decode_admits(self.request))

    def prefilling(self, count, wanted_len=0, input_len=10):
        for _ in range(count):
            rid = f"p{len(self.scheduler.monitor.instances['e0'].prefill)}"
            self.scheduler.monitor.dispatched("e0", Request(rid, input_len, wanted_len=wanted_len))

    def ready(self, request):
        prefill = self.scheduler.monitor.instances["e0"]
        return self.scheduler.prefill_admission_price(request, prefill)

    def test_a_serving_concurrency_limit_caps_the_measured_limit(self):
        self.fill(2)
        self.assertFalse(self.scheduler.decode_admits(self.request, concurrency=2))
        self.assertTrue(self.scheduler.decode_admits(self.request, concurrency=3))

    def test_requests_in_prefill_count_when_they_start_decode_inside_the_window(self):
        limit = self.scheduler.profiles.get("e3").decode_max_requests
        self.fill(limit - 3, wanted_len=2_000)
        self.prefilling(3, wanted_len=2_000, input_len=8_000)
        request = Request("new", 10, wanted_len=1)
        self.assertTrue(self.scheduler.decode_admits(request))
        ready = self.ready(request)
        self.assertFalse(self.scheduler.decode_admits(request, ready_s=ready))

    def test_requests_in_prefill_that_finish_decode_by_ready_time_hold_no_slot(self):
        limit = self.scheduler.profiles.get("e3").decode_max_requests
        self.fill(limit - 1, wanted_len=2_000)
        self.prefilling(3, wanted_len=1)
        self.assertTrue(
            self.scheduler.decode_admits(self.request, ready_s=self.ready(self.request) + 5.0)
        )

    def test_the_demand_model_estimates_output_from_the_requested_cap_or_reports_unknown(self):
        estimate = self.router.controller.demand.output_estimator()
        self.assertEqual(estimate(Request("r", 10, wanted_len=64)), 64)
        self.assertEqual(estimate(Request("r", 10)), 0)

    def test_residents_that_finish_before_ready_time_hold_no_slot(self):
        self.fill(self.scheduler.profiles.get("e3").decode_max_requests, wanted_len=2)
        self.assertFalse(self.scheduler.decode_admits(self.request))
        self.assertTrue(self.scheduler.decode_admits(self.request, ready_s=10.0))

    def test_a_burst_admits_up_to_projected_decode_capacity(self):
        self.fill(self.scheduler.profiles.get("e3").decode_max_requests - 1, wanted_len=2_000)
        admitted = 0
        for index in range(50):
            arrival = Request(f"a{index}", 10, wanted_len=2_000)
            if self.scheduler.decode_admits(arrival, ready_s=self.ready(arrival)):
                admitted += 1
                self.scheduler.monitor.dispatched("e0", arrival)
        self.assertEqual(admitted, 1)

    def test_expected_output_sizes_decode_tokens(self):
        for index in range(6):
            self.scheduler.monitor.dispatched(
                "e3", Request(f"d{index}", 500, phase=Phase.DECODE, wanted_len=32_768)
            )
        request = Request("new", 500, wanted_len=32_768)
        self.assertFalse(self.scheduler.decode_admits(request))
        self.assertTrue(self.scheduler.decode_admits(request, expected_output=lambda r: 300))

    def test_residents_and_the_request_share_the_token_budget(self):
        request = Request("long", 30_000, wanted_len=20_000)
        for index in range(5):
            self.scheduler.monitor.dispatched("e3", Request(f"s{index}", 500, phase=Phase.DECODE))
        self.assertTrue(self.scheduler.decode_admits(request))
        for index in range(2):
            self.scheduler.monitor.dispatched(
                "e3", Request(f"l{index}", 30_000, phase=Phase.DECODE, wanted_len=20_000)
            )
        self.assertFalse(self.scheduler.decode_admits(request))

    def test_a_resident_past_its_estimate_holds_until_its_cap(self):
        limit = self.scheduler.profiles.get("e3").decode_max_requests
        for index in range(limit):
            self.scheduler.monitor.dispatched(
                "e3", Request(f"r{index}", 10, phase=Phase.DECODE, output_len=150, wanted_len=2_000)
            )
        estimate = lambda r: 100
        self.assertFalse(
            self.scheduler.decode_admits(self.request, ready_s=10.0, expected_output=estimate)
        )

    def test_a_resident_with_an_unknown_output_holds_its_slot(self):
        self.fill(self.scheduler.profiles.get("e3").decode_max_requests)
        self.assertFalse(
            self.scheduler.decode_admits(self.request, ready_s=10.0, expected_output=lambda r: 0)
        )

    def test_delivered_output_shortens_a_residents_hold(self):
        limit = self.scheduler.profiles.get("e3").decode_max_requests
        for index in range(limit):
            self.scheduler.monitor.dispatched(
                "e3",
                Request(f"r{index}", 10, phase=Phase.DECODE, output_len=1_999, wanted_len=2_000),
            )
        self.assertTrue(self.scheduler.decode_admits(self.request, ready_s=1.0))
        for request in self.scheduler.monitor.instances["e3"].decode.values():
            request.output_len = 0
        self.assertFalse(self.scheduler.decode_admits(self.request, ready_s=1.0))

    def test_requests_reaching_decode_during_the_window_count_at_their_peak(self):
        limit = self.scheduler.profiles.get("e3").decode_max_requests
        self.prefilling(limit, wanted_len=2_000, input_len=1_000)
        request = Request("new", 10, wanted_len=2_000)
        self.assertFalse(self.scheduler.decode_admits(request, ready_s=0.0))
        short = Request("short", 10, wanted_len=1)
        self.assertTrue(self.scheduler.decode_admits(short, ready_s=0.0))

    def test_the_tpot_check_admits_on_any_engine_with_room(self):
        self.scheduler.monitor.instances["e0"].role = Role.DECODE
        for iid in ("e0", "e3"):
            self.scheduler.profiles.put(
                replace(
                    self.scheduler.profiles.get(iid),
                    kv_capacity_tokens=2_000_000,
                    decode_max_kv_tokens=2_000_000,
                )
            )
        self.scheduler.monitor.dispatched(
            "e0", Request("long", 480_000, phase=Phase.DECODE, wanted_len=2_000)
        )
        for index in range(2):
            self.scheduler.monitor.dispatched(
                "e3", Request(f"s{index}", 12_000, phase=Phase.DECODE, wanted_len=2_000)
            )
        self.assertTrue(self.scheduler.decode_admits(Request("new", 20_000, wanted_len=8)))

    def test_the_tpot_check_counts_residents_generating_at_ready_time(self):
        self.scheduler.monitor.instances["e0"].role = Role.DECODE
        for iid in ("e0", "e3"):
            self.scheduler.profiles.put(
                replace(
                    self.scheduler.profiles.get(iid),
                    kv_capacity_tokens=1_000_000,
                    decode_max_kv_tokens=1_000_000,
                )
            )
        for index in range(3):
            self.scheduler.monitor.dispatched(
                "e0", Request(f"a{index}", 150_000, phase=Phase.DECODE, wanted_len=1)
            )
        for index in range(2):
            self.scheduler.monitor.dispatched(
                "e3", Request(f"b{index}", 200_000, phase=Phase.DECODE, wanted_len=2_000)
            )
        request = Request("new", 200_000, wanted_len=1)
        self.assertFalse(self.scheduler.decode_admits(request))
        self.assertTrue(self.scheduler.decode_admits(request, ready_s=2.0))

    def test_admission_reuses_the_controller_estimate_snapshot(self):
        demand = self.router.controller.demand
        demand.refresh_output_estimates()
        with patch.object(demand, "_output_estimates", wraps=demand._output_estimates) as build:
            demand.output_estimator()
            demand.output_estimator()
        self.assertEqual(build.call_count, 0)

    def test_each_request_holds_its_final_length_in_kv_tokens(self):
        for index in range(4):
            self.scheduler.monitor.dispatched(
                "e3", Request(f"r{index}", 500, phase=Phase.DECODE, wanted_len=20_000)
            )
        self.assertFalse(self.scheduler.decode_admits(Request("new", 500, wanted_len=20_000)))
        self.assertTrue(self.scheduler.decode_admits(Request("new", 500, wanted_len=17_000)))

    def test_an_uncapped_request_checks_decode_at_its_prefill_completion(self):
        limit = self.scheduler.profiles.get("e3").decode_max_requests
        self.prefilling(limit, wanted_len=2_000, input_len=1_000)
        self.assertTrue(self.scheduler.decode_admits(Request("new", 10), ready_s=0.0))
        self.assertFalse(self.scheduler.decode_admits(Request("new", 10), ready_s=20.0))

    def test_a_resident_at_its_cap_frees_its_slot(self):
        limit = self.scheduler.profiles.get("e3").decode_max_requests
        for index in range(limit):
            self.scheduler.monitor.dispatched(
                "e3", Request(f"r{index}", 10, phase=Phase.DECODE, output_len=8, wanted_len=8)
            )
        self.assertTrue(self.scheduler.decode_admits(self.request, ready_s=0.5))

    def test_a_resident_past_its_estimate_holds_kv_to_its_cap(self):
        for index in range(5):
            self.scheduler.monitor.dispatched(
                "e3",
                Request(f"r{index}", 500, phase=Phase.DECODE, output_len=600, wanted_len=20_000),
            )
        request = Request("new", 500, wanted_len=20_000)
        self.assertFalse(self.scheduler.decode_admits(request, expected_output=lambda r: 500))

    def test_requests_that_decode_at_different_times_share_slots(self):
        limit = self.scheduler.profiles.get("e3").decode_max_requests
        self.prefilling(limit, wanted_len=1, input_len=1_000)
        self.assertTrue(self.scheduler.decode_admits(Request("new", 10, wanted_len=2_000)))

    def test_the_output_estimate_falls_back_to_the_fleet_median(self):
        demand = self.router.controller.demand
        for observed in (40, 50, 60):
            demand.saw_completion(100, 0, observed)
        demand.refresh_output_estimates()
        estimate = demand.output_estimator()
        self.assertEqual(estimate(Request("r", 5_000)), 50)
        self.assertEqual(estimate(Request("r", 5_000, wanted_len=64)), 64)

    def test_output_estimates_outlast_a_completion_history_overflow(self):
        """A burst of distinct completion shapes keeps the last learned estimates."""
        demand = self.router.controller.demand
        now = self.router._clock()
        for _ in range(20):
            demand.saw_completion(500, 8192, 200, at=now)
        demand.refresh_output_estimates()
        request = Request("r", 500, wanted_len=8192)
        self.assertEqual(demand.output_estimator()(request), 200)
        for index in range(129):
            demand.saw_completion(100 + index, 64, 10 + index % 7, at=now)
        demand.refresh_output_estimates()
        self.assertTrue(any(row.overflow for row in demand.observed_decode.rows()))
        self.assertEqual(demand.output_estimator()(request), 200)

    def test_requests_waiting_for_a_decode_slot_count_as_committed(self):
        self.fill(self.scheduler.profiles.get("e3").decode_max_requests - 1)
        self.assertTrue(self.scheduler.decode_admits(self.request))
        self.scheduler.monitor.waiting["w"] = Request("w", 10, phase=Phase.DECODE)
        self.assertFalse(self.scheduler.decode_admits(self.request))

    def test_decode_admission_follows_the_tpot_budget_under_load(self):
        self.scheduler.profiles.put(
            replace(
                self.scheduler.profiles.get("e3"),
                kv_capacity_tokens=2_000_000,
                decode_max_kv_tokens=2_000_000,
            )
        )
        for index in range(3):
            self.scheduler.monitor.dispatched(
                "e3", Request(f"kv{index}", 150_000, phase=Phase.DECODE, wanted_len=1)
            )
        self.assertFalse(self.scheduler.decode_admits(Request("new", 200_000, wanted_len=1)))
        self.assertTrue(self.scheduler.decode_admits(Request("new", 40_000, wanted_len=1)))

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
