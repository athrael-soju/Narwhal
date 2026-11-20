"""Check prefill placement on decode-role engines inside their measured TPOT envelope."""

import random
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from narwhal.profiling.fitting import fit_prefill_slowdown, prefill_share
from narwhal.profiling.store import ProfileStore
from narwhal.scheduling.control import SLO, Thresholds
from narwhal.scheduling.monitor import InstanceMonitor
from narwhal.scheduling.prefill import prefill_seconds
from narwhal.scheduling.scheduler.occupancy import DecodeCheck
from narwhal.scheduling.scheduler.placement import GlobalScheduler
from narwhal.serving import execution
from narwhal.serving.admission import QueueExpired
from narwhal.serving.app import create_app
from narwhal.serving.policy import ServingPolicy
from narwhal.types import Instance, Phase, Request, Role
from tests.fixtures import fleet, profile, warm
from tests.scheduling.test_reactive import Fleet
from tests.serving.test_http_accounting import HttpHarness

# Prefill takes 0.001 s per prompt token plus 0.01 s; one resident of 1,100 tokens decodes at
# 3.1 ms per token.
ENVELOPE = {"decode_prefill_slowdown": 0.8, "decode_prefill_max_share": 0.5}


class DecodeEnginePrefillTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.now = [100.0]
        self.profiles = ProfileStore(Path(folder.name) / "profiles.json")
        self.monitor = InstanceMonitor(clock=lambda: self.now[0], profiles=self.profiles)
        for iid, role, fields in (
            ("p", Role.PREFILL, {}),
            ("d", Role.DECODE, ENVELOPE),
            ("q", Role.DECODE, {}),
        ):
            self.profiles.put(profile(iid, **fields))
            self.monitor.add(Instance(iid, f"http://{iid}", role))
        self.scheduler = GlobalScheduler(
            self.monitor,
            self.profiles,
            SLO(2.0, 0.05),
            Thresholds(),
            clock=lambda: self.now[0],
            prefill_on_decode=True,
        )

    def resident(self, iid, rid, *, delivered, wanted, decoding_s):
        """Seat a decode stream on `iid` that has decoded `delivered` tokens in `decoding_s`."""
        request = Request(rid, 1000, phase=Phase.DECODE, wanted_len=wanted)
        self.monitor.dispatched(iid, request)
        self.now[0] -= decoding_s
        self.monitor.first_token(iid, rid)
        self.now[0] += decoding_s
        request.output_len = delivered
        return request

    def backlog(self, tokens=3000):
        """Queue prefill on the prefill engine so a new leg there misses the TTFT budget."""
        self.monitor.dispatched("p", Request("backlog", tokens))

    def hosts(self, request):
        pool, borrowed = self.scheduler.prefill_pool(request, self.scheduler.live_instances())
        self.assertEqual(borrowed, {inst.iid for inst in pool if inst.role is Role.DECODE})
        return [inst.iid for inst in pool]

    def test_a_decode_engine_takes_a_leg_inside_its_envelope_only_when_prefill_misses_ttft(self):
        self.resident("d", "long", delivered=100, wanted=1100, decoding_s=0.31)
        self.assertEqual(self.hosts(Request("leg", 1000)), ["p", "d"])
        self.assertEqual(self.scheduler.schedule(Request("leg", 1000)).iid, "p")
        self.backlog()
        self.assertEqual(self.scheduler.schedule(Request("leg", 1000)).iid, "d")
        self.assertEqual(self.scheduler.unserved, 0)

    def test_an_idle_decode_engine_ranks_after_a_prefill_engine_that_meets_ttft(self):
        # A 0.21 s backlog leaves the prefill engine at 1.22 s, inside the 2 s budget, while the
        # idle decode engine prices the leg at 1.01 s with no resident decode tokens.
        self.backlog(200)
        self.assertEqual(self.hosts(Request("leg", 1000)), ["p", "d"])
        self.assertEqual(self.scheduler.schedule(Request("leg", 1000)).iid, "p")
        # A borrowed engine that also misses the budget leaves the leg on the prefill engine.
        self.monitor.dispatched("p", Request("more", 3000))
        self.monitor.dispatched("d", Request("queued", 1500))
        self.assertEqual(self.hosts(Request("leg", 1000)), ["p", "d"])
        self.assertEqual(self.scheduler.schedule(Request("leg", 1000)).iid, "p")
        self.assertEqual(self.scheduler.unserved, 1)

    def test_the_setting_off_or_a_profile_without_the_fit_keeps_decode_engines_out(self):
        self.resident("d", "long", delivered=100, wanted=1100, decoding_s=0.31)
        self.backlog()
        self.scheduler.prefill_on_decode = False
        self.assertEqual(self.hosts(Request("leg", 1000)), ["p"])
        self.assertEqual(self.scheduler.schedule(Request("leg", 1000)).iid, "p")
        self.assertEqual(self.scheduler.unserved, 1)
        self.scheduler.prefill_on_decode = True
        self.profiles.put(profile("d"))
        self.assertEqual(self.hosts(Request("leg", 1000)), ["p"])

    def test_without_a_prefill_engine_a_borrowed_engine_takes_the_leg(self):
        self.scheduler.eject("p", "liveness")
        self.resident("d", "short", delivered=2, wanted=12, decoding_s=0.01)
        self.assertEqual(self.hosts(Request("leg", 10)), ["d"])
        self.assertEqual(self.scheduler.schedule(Request("leg", 10)).iid, "d")
        # Admission prices the borrowed engine at its placement cost.
        d = self.monitor.instances["d"]
        self.assertAlmostEqual(self.scheduler.prefill_admission_price(Request("leg", 10), d), 0.02)
        # Queued pricing passes the pool's borrowed set and prices the same way.
        for leg in (10, 1000):
            request = Request("leg", leg)
            _, borrowed = self.scheduler.prefill_pool(request, self.scheduler.live_instances())
            self.assertEqual(
                self.scheduler.prefill_admission_price(request, d, borrowed),
                self.scheduler.prefill_admission_price(request, d),
            )

    def test_without_a_prefill_or_borrowed_engine_placement_falls_back_as_with_the_setting_off(
        self,
    ):
        self.scheduler.eject("p", "liveness")
        self.resident("d", "short", delivered=2, wanted=12, decoding_s=0.01)
        d, q = self.monitor.instances["d"], self.monitor.instances["q"]
        placed = {}
        for enabled in (False, True):
            self.scheduler.prefill_on_decode = enabled
            pool, borrowed = self.scheduler.prefill_pool(
                Request("leg", 1000), self.scheduler.live_instances()
            )
            self.assertEqual(([inst.iid for inst in pool], borrowed), (["d", "q"], set()))
            placed[enabled] = (
                self.scheduler.schedule(Request("leg", 1000)).iid,
                # A fallback decode engine with residents keeps its infinite admission price.
                self.scheduler.prefill_admission_price(Request("leg", 1000), d),
                self.scheduler.prefill_admission_price(Request("leg", 1000), q),
            )
        self.assertEqual(placed[True], placed[False])
        self.assertEqual(placed[True][:2], ("q", float("inf")))

    def test_a_retry_that_excludes_the_prefill_engine_falls_back_with_the_setting_on(self):
        self.profiles.put(profile("d"))
        for enabled in (False, True):
            self.scheduler.prefill_on_decode = enabled
            self.assertEqual(self.scheduler.schedule(Request("r", 1000), exclude={"p"}).iid, "d")

    def test_held_pinned_and_unavailable_decode_engines_take_no_leg(self):
        self.backlog()
        self.scheduler.hold_for_switch("d")
        self.assertEqual(self.hosts(Request("leg", 1000)), ["p"])
        self.scheduler.switch_holds.clear()
        self.scheduler.pinned = frozenset({"d"})
        self.assertEqual(self.hosts(Request("leg", 1000)), ["p"])
        self.scheduler.pinned = frozenset()
        self.scheduler.drain("d")
        self.assertEqual(self.hosts(Request("leg", 1000)), ["p"])
        self.scheduler.finish_drain("d")
        self.assertEqual(self.scheduler.schedule(Request("leg", 1000), exclude={"d"}).iid, "p")
        self.assertEqual(self.hosts(Request("leg", 1000)), ["p", "d"])

    def test_a_decode_engine_with_no_other_decode_engine_takes_no_leg(self):
        # Its own decode leg would land on it and hold the handoff blocks until the lease ends.
        self.backlog()
        self.scheduler.eject("q", "liveness")
        self.assertEqual(self.hosts(Request("leg", 1000)), ["p"])
        self.assertEqual(self.scheduler.schedule(Request("leg", 1000)).iid, "p")
        self.scheduler.availability.ejected.pop("q")
        producer = self.scheduler.schedule(Request("leg", 1000))
        self.assertEqual(producer.iid, "d")
        decode = Request("leg", 1000, phase=Phase.DECODE, prefill_instance="d")
        self.assertEqual(self.scheduler.schedule(decode).iid, "q")

    def test_a_short_output_resident_blocks_a_long_leg_and_admits_a_short_one(self):
        self.backlog()
        # Ten tokens left at 3 ms: a 1.01 s leg lifts the mean TPOT to
        # (0.01 + 0.03 + 0.8 * 1.01) / 11 = 77 ms.
        self.resident("d", "short", delivered=2, wanted=12, decoding_s=0.01)
        self.assertEqual(self.hosts(Request("leg", 1000)), ["p"])
        self.profiles.put(profile("d", decode_prefill_slowdown=0.8, decode_prefill_max_share=0.99))
        self.assertEqual(self.hosts(Request("leg", 1000)), ["p"])
        # A 0.02 s leg keeps the mean TPOT at 5 ms.
        self.assertEqual(self.hosts(Request("leg", 10)), ["p", "d"])

    def test_the_share_cap_uses_the_residents_mean_remaining_decode_time(self):
        self.backlog()
        self.resident("d", "long", delivered=100, wanted=1100, decoding_s=0.31)
        self.assertEqual(self.hosts(Request("leg", 1000)), ["p", "d"])
        # A queued 4 s prefill lifts the stall to 5.02 s, 61% beside the 3.2 s mean span.
        self.monitor.dispatched("d", Request("queued", 4000))
        self.assertEqual(self.hosts(Request("leg", 1000)), ["p"])

    def test_a_resident_at_its_cap_counts_zero_decode_time_toward_the_share(self):
        self.backlog()
        ending = self.resident("d", "ending", delivered=100, wanted=100, decoding_s=0.31)
        # Alone, it leaves the stall with no decode time beside it.
        self.assertEqual(self.hosts(Request("leg", 1000)), ["p"])
        # Beside a resident with 3.2 s left, the mean span is 1.6 s and the share 39%.
        self.resident("d", "long", delivered=100, wanted=1100, decoding_s=0.31)
        self.assertEqual(self.hosts(Request("leg", 1000)), ["p", "d"])
        self.assertEqual(ending.output_len, ending.wanted_len)

    def test_the_controller_output_estimate_sets_each_residents_remaining_output(self):
        self.backlog()
        long = self.resident("d", "long", delivered=100, wanted=1100, decoding_s=0.31)
        self.scheduler.output_estimator = lambda: lambda r: 110 if r is long else 0
        self.assertEqual(self.hosts(Request("leg", 1000)), ["p"])
        # Without an estimate or a requested cap, the remaining output is unknown.
        self.scheduler.output_estimator = lambda: lambda r: 0
        long.wanted_len = 0
        self.assertEqual(self.hosts(Request("leg", 1000)), ["p"])

    def test_the_leg_keeps_the_engine_inside_its_measured_kv_range(self):
        self.backlog()
        self.profiles.put(profile("d", decode_max_kv_tokens=2000, **ENVELOPE))
        self.resident("d", "long", delivered=100, wanted=1100, decoding_s=0.31)
        self.assertEqual(self.hosts(Request("leg", 1000)), ["p"])
        self.assertEqual(self.hosts(Request("leg", 900)), ["p", "d"])
        # Resident prefill tokens count toward the range.
        self.monitor.dispatched("d", Request("queued", 100))
        self.assertEqual(self.hosts(Request("leg", 900)), ["p"])

    def test_placement_skips_the_borrow_check_only_when_it_cannot_change_the_choice(self):
        rng = random.Random(7)
        checked = settled = 0
        for trial in range(300):
            with self.subTest(trial=trial):
                self.setUp()
                self.profiles.put(
                    warm("p", cached_max_prefix_tokens=2000, cached_max_suffix_tokens=2000)
                    if rng.random() < 0.5
                    else profile("p", ttft_b=0.002)
                )
                for k in range(rng.randrange(4)):
                    self.monitor.dispatched("p", Request(f"b{k}", rng.choice((100, 600, 1500))))
                for k in range(rng.randrange(3)):
                    left = rng.choice((10, 400, 1000))
                    self.resident("d", f"r{k}", delivered=50, wanted=50 + left, decoding_s=0.2)
                if rng.random() < 0.2:
                    self.scheduler.hold_for_switch(rng.choice(("p", "d")))
                leg = rng.choice((10, 300, 700, 1200))
                cached = {"p": rng.choice((32, 300))} if rng.random() < 0.5 else {}
                lazy = Request("leg", leg, cached_tokens=dict(cached))
                full = Request("leg", leg, cached_tokens=dict(cached))
                settled += (
                    self.scheduler.settle_on_own(lazy, self.scheduler.live_instances(), None)
                    is not None
                )
                before = self.scheduler.unserved
                chosen = self.scheduler.schedule(lazy).iid
                lazy_unserved = self.scheduler.unserved - before
                with patch.object(GlobalScheduler, "settle_on_own", return_value=None):
                    expected = self.scheduler.schedule(full).iid
                self.assertEqual(
                    (chosen, lazy_unserved, lazy.cache_placement),
                    (
                        expected,
                        self.scheduler.unserved - before - lazy_unserved,
                        full.cache_placement,
                    ),
                )
                checked += 1
        # Both paths ran: some placements settle on the prefill engine and some do not.
        self.assertEqual(checked, 300)
        self.assertGreater(settled, 30)
        self.assertLess(settled, 270)

    def test_placement_caps_the_share_the_profiler_measured(self):
        """The profiler's share and placement's share agree on one stall beside one span."""
        base, tokens, slowdown, stall = 0.0031, 1000, 0.8, 1.01
        window = tokens * base + slowdown * stall
        fitted, measured = fit_prefill_slowdown(
            [(0.0, 10.0, base), (stall, window, window / tokens)]
        )
        self.assertAlmostEqual(fitted, slowdown)
        # The resident has 1,000 tokens left at 3.1 ms and the leg costs 1.01 s.
        self.assertAlmostEqual(measured, prefill_share(stall, tokens * base))
        self.backlog()
        self.resident("d", "long", delivered=100, wanted=1100, decoding_s=0.31)
        for cap, hosts in ((measured + 1e-9, ["p", "d"]), (measured - 1e-9, ["p"])):
            self.profiles.put(
                profile("d", decode_prefill_slowdown=fitted, decode_prefill_max_share=cap)
            )
            self.assertEqual(self.hosts(Request("leg", 1000)), hosts)


class BorrowedStallTests(unittest.TestCase):
    """d decodes four streams at 4 ms; its fit prices 0.8 s of stall per prefill second."""

    def setUp(self):
        case = DecodeEnginePrefillTests(
            "test_held_pinned_and_unavailable_decode_engines_take_no_leg"
        )
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.monitor, self.scheduler, self.now = case.monitor, case.scheduler, case.now
        self.d = self.monitor.instances["d"]
        for k in range(4):
            self.monitor.dispatched("d", Request(f"r{k}", 1000, phase=Phase.DECODE, wanted_len=500))
            self.monitor.first_token("d", f"r{k}")
        # The correction settles at its lower bound.
        self.decode(60, 0.004)
        self.base = (self.scheduler.decode_load(self.d), self.monitor.decode_correction("d"))

    def decode(self, intervals, gap):
        for _ in range(intervals):
            for _ in range(10):
                self.now[0] += gap
                for k in range(4):
                    self.monitor.output_token("d", f"r{k}")
            self.monitor.roll_interval()

    def lend(self, borrowed=True):
        leg = Request("leg", 1000, borrowed=borrowed)
        self.monitor.dispatched("d", leg)
        # The leg's profiled prefill time, the span the envelope priced.
        self.assertAlmostEqual(prefill_seconds(self.scheduler.profiles.get("d"), leg), 1.01)
        return leg

    def release(self):
        self.monitor.first_token("d", "leg")
        self.monitor.finished("d", "leg")

    def test_a_stall_within_the_priced_slowdown_leaves_decode_load_unchanged(self):
        loads = {}
        for borrowed in (True, False):
            with self.subTest(borrowed=borrowed):
                self.setUp()
                self.lend(borrowed)
                # Five times the 4 ms gap: 16 ms of each 20 ms gap is the priced 0.8 share.
                self.decode(5, 0.02)
                loads[borrowed] = self.scheduler.decode_load(self.d)
                self.release()
                self.decode(10, 0.004)
                self.assertAlmostEqual(self.scheduler.decode_load(self.d), self.base[0])
                self.assertAlmostEqual(self.monitor.decode_correction("d"), self.base[1], 4)
        self.assertAlmostEqual(loads[True], self.base[0])
        # Placed without the envelope, the same slowdown counts in full.
        self.assertGreater(loads[False], 4 * self.base[0])

    def test_an_overload_beside_a_borrowed_leg_raises_decode_load(self):
        self.lend()
        # Forty times the 4 ms gap: 0.8 of it is priced, and 128 ms per gap still counts.
        self.decode(2, 0.16)
        self.assertGreater(self.scheduler.decode_load(self.d), 30 * self.base[0])

    def test_a_hang_beside_a_borrowed_leg_shows_and_survives_its_release(self):
        self.lend()
        self.now[0] += 10.0
        # The leg's 1.01 s span prices 0.808 s of each open gap.
        self.assertAlmostEqual(self.monitor.stalled_gap("d"), 10.0 - 0.808)
        self.now[0] += 20.0
        self.assertAlmostEqual(self.monitor.stalled_gap("d"), 30.0 - 0.808)
        self.release()
        self.monitor.roll_interval()
        self.assertAlmostEqual(self.monitor.stalled_gap("d"), 30.0 - 0.808)
        self.assertGreater(self.scheduler.decode_load(self.d), 500)

    def test_back_to_back_legs_price_one_span_after_another(self):
        for index in range(3):
            self.monitor.dispatched("d", Request(f"leg{index}", 1000, borrowed=True))
        self.now[0] += 10.0
        self.assertAlmostEqual(self.monitor.stalled_gap("d"), 10.0 - 3 * 0.808)
        for index in range(3):
            self.monitor.first_token("d", f"leg{index}")
            self.monitor.finished("d", f"leg{index}")
        self.decode(1, 0.004)
        self.monitor.roll_interval()
        # Spans no decode gap can still overlap are dropped.
        self.assertEqual(self.monitor._lent, {})

    def test_borrowed_prefill_counts_toward_the_prefill_pool_but_not_its_engine_count(self):
        case = DecodeEnginePrefillTests(
            "test_held_pinned_and_unavailable_decode_engines_take_no_leg"
        )
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.monitor.dispatched("p", Request("own", 990))
        case.monitor.dispatched("d", Request("lent", 990, borrowed=True))
        # Each engine holds one second of prefill against a 2 s TTFT target.
        self.assertAlmostEqual(case.scheduler.pool_load(Role.PREFILL), 1.0)
        case.monitor.dispatched("q", Request("fallback", 990))
        self.assertAlmostEqual(case.scheduler.pool_load(Role.PREFILL), 1.5 / 2)

    def test_a_controller_pass_with_borrowed_stalls_does_not_move_toward_decode(self):
        moves = {}
        for borrowed in (False, True):
            folder = tempfile.TemporaryDirectory()
            self.addCleanup(folder.cleanup)
            fleet = Fleet(folder.name)
            fleet.telemetry.stop()
            fleet.monitor.instances["e1"].role = Role.PREFILL
            # The fit prices a stall that pauses decode for the whole of a 10 ms/token prefill.
            fleet.profiles.put(
                replace(
                    fleet.profiles.get("e2"),
                    ttft_b=0.01,
                    decode_prefill_slowdown=1.0,
                    decode_prefill_max_share=0.5,
                )
            )
            monitor = fleet.monitor
            for k in range(4):
                monitor.dispatched("e2", Request(f"r{k}", 100, phase=Phase.DECODE, wanted_len=100))
                monitor.first_token("e2", f"r{k}")
            for _ in range(20):
                fleet.now += 0.002
                for k in range(4):
                    monitor.output_token("e2", f"r{k}")
            monitor.roll_interval()
            # A 50 s prefill leg on the decode engine stalls its streams through the test.
            monitor.dispatched("e2", Request("leg", 5000, wanted_len=10, borrowed=borrowed))
            moves[borrowed] = []
            with patch.object(fleet.scheduler, "prefill_load", return_value=0.7):
                for _ in range(6):
                    fleet.advance(5)
                    monitor.roll_interval()
                    before = len(monitor.pool(Role.PREFILL))
                    fleet.controller.step()
                    after = len(monitor.pool(Role.PREFILL))
                    if after != before:
                        moves[borrowed].append(after - before)
        # The unpriced stall reads as decode pressure and moves a prefill engine to decode.
        self.assertEqual(moves[False][0], -1)
        self.assertTrue(moves[True])
        self.assertTrue(all(step > 0 for step in moves[True]))


class DecodeEnginePrefillJournalTests(HttpHarness):
    async def test_the_journal_records_the_role_of_the_engine_that_ran_prefill(self):
        # A second decode engine takes the decode leg the borrowed engine produces.
        self.cfg.engines = fleet(self.root, engines=("e0", "e3", "e4")).engines
        self.cfg.serving = ServingPolicy(prefill_on_decode=True)
        ProfileStore(self.cfg.profiles_path).put(profile("e3", **ENVELOPE))
        client = self.client()
        monitor = self.router.monitor
        with patch.object(monitor, "_lend", wraps=monitor._lend) as lent:
            self.assertEqual((await self.post(client)).status_code, 200)
            # A prefill engine held for a switch leaves the leg to the decode engine.
            self.router.scheduler.hold_for_switch("e0")
            self.assertEqual((await self.post(client)).status_code, 200)
        rows = self.terminal_rows()
        self.assertEqual(
            [(row["prefill_iid"], row["prefill_role"]) for row in rows],
            [("e0", "prefill"), ("e3", "decode")],
        )
        # The monitor saw the second leg as borrowed on e3.
        self.assertEqual([call.args[0] for call in lent.call_args_list], ["e3"])
        self.assert_released()


class DecodeEnginePrefillRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_queued_prefill_leg_waits_for_a_decode_engine_only_with_the_setting(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                folder = tempfile.TemporaryDirectory()
                self.addCleanup(folder.cleanup)
                cfg = fleet(Path(folder.name), engines=("e0", "e3", "e4"))
                ProfileStore(cfg.profiles_path).put(profile("e3", **ENVELOPE))
                cfg.serving = replace(
                    ServingPolicy(queue_capacity=4, queue_timeout_s=5.0),
                    prefill_on_decode=enabled,
                )
                router = create_app(cfg).state.router
                self.addAsyncCleanup(router.engines.aclose)
                self.assertIs(router.scheduler.prefill_on_decode, enabled)
                self.assertEqual(
                    router.scheduler.output_estimator, router.controller.demand.output_estimator
                )
                place = router.dispatcher.place(
                    Request("r", 10),
                    deadline=router._clock() + 0.05,
                    claim=lambda inst: None,
                    exclude={"e0"},
                )
                if enabled:
                    self.assertEqual((await place).iid, "e3")
                else:
                    with self.assertRaises(QueueExpired):
                        await place

    async def test_a_waiting_leg_is_priced_on_the_engine_placement_would_choose(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        cfg = fleet(Path(folder.name), engines=("e0", "e3", "e4"))
        ProfileStore(cfg.profiles_path).put(profile("e3", **ENVELOPE))
        cfg.admission = "predictive"
        cfg.slo = replace(cfg.slo, ttft_s=2.0, tpot_s=0.05)
        cfg.serving = ServingPolicy(prefill_on_decode=True)
        router = create_app(cfg).state.router
        self.addAsyncCleanup(router.engines.aclose)
        state = SimpleNamespace(
            router=router,
            request=Request("r", 1000),
            attempts=0,
            failed_engines={"prefill": set()},
            arrived=router._clock(),
            admission_price=None,
        )
        priced = []
        with (
            patch.object(
                execution, "_price_admission", side_effect=lambda s, inst: priced.append(inst.iid)
            ),
            patch.object(execution, "_check_ttft"),
            patch.object(execution, "_decode_slot_check", return_value=DecodeCheck(None)),
        ):
            # The idle decode engine is cheaper, and the prefill engine still meets the budget.
            router.monitor.dispatched("e0", Request("backlog", 200))
            execution.price_waiting(state)
            with patch.object(GlobalScheduler, "own_prefill_meets", return_value=None):
                execution.price_waiting(state)
            router.monitor.dispatched("e0", Request("more", 3000))
            execution.price_waiting(state)
            with patch.object(GlobalScheduler, "own_prefill_meets", return_value=None):
                execution.price_waiting(state)
        # The check that skips the borrowed engines prices as the full tier does.
        self.assertEqual(priced, ["e0", "e0", "e3", "e3"])
