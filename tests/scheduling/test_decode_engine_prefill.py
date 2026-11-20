"""Check prefill placement on decode-role engines inside their measured TPOT envelope."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from narwhal.profiling.store import ProfileStore
from narwhal.scheduling.control import SLO, Thresholds
from narwhal.scheduling.monitor import InstanceMonitor
from narwhal.scheduling.scheduler.placement import GlobalScheduler
from narwhal.serving.admission import QueueExpired
from narwhal.serving.app import create_app
from narwhal.serving.policy import ServingPolicy
from narwhal.types import Instance, Phase, Request, Role
from tests.fixtures import fleet, profile
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
        pool = self.scheduler.prefill_pool(request, self.scheduler.live_instances())
        return [inst.iid for inst in pool]

    def test_a_decode_engine_takes_a_leg_inside_its_envelope_only_when_prefill_misses_ttft(self):
        self.resident("d", "long", delivered=100, wanted=1100, decoding_s=0.31)
        self.assertEqual(self.hosts(Request("leg", 1000)), ["p", "d"])
        # The prefill engine meets the TTFT budget and orders first: the decode engine holds
        # resident decode tokens.
        self.assertEqual(self.scheduler.schedule(Request("leg", 1000)).iid, "p")
        self.backlog()
        self.assertEqual(self.scheduler.schedule(Request("leg", 1000)).iid, "d")

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

    def test_a_short_output_resident_blocks_a_long_leg_and_admits_a_short_one(self):
        self.backlog()
        # Ten tokens left at 3 ms: a 1.01 s leg takes 97% of that span.
        self.resident("d", "short", delivered=2, wanted=12, decoding_s=0.01)
        self.assertEqual(self.hosts(Request("leg", 1000)), ["p"])
        # A 0.02 s leg takes 40% of it and keeps the mean TPOT at 5 ms.
        self.assertEqual(self.hosts(Request("leg", 10)), ["p", "d"])
        # Inside a wider measured share, the long leg lifts the mean TPOT to
        # (0.01 + 0.03 + 0.8 * 1.01) / 11 = 77 ms.
        self.profiles.put(profile("d", decode_prefill_slowdown=0.8, decode_prefill_max_share=0.99))
        self.assertEqual(self.hosts(Request("leg", 1000)), ["p"])

    def test_resident_prefill_on_the_engine_adds_to_the_stall(self):
        self.backlog()
        self.resident("d", "long", delivered=100, wanted=1100, decoding_s=0.31)
        self.assertEqual(self.hosts(Request("leg", 1000)), ["p", "d"])
        # A queued 4 s prefill on the decode engine lifts the stall to 62% of the span.
        self.monitor.dispatched("d", Request("queued", 4000))
        self.assertEqual(self.hosts(Request("leg", 1000)), ["p"])

    def test_the_controller_output_estimate_sets_each_residents_remaining_output(self):
        self.backlog()
        long = self.resident("d", "long", delivered=100, wanted=1100, decoding_s=0.31)
        self.scheduler.output_estimator = lambda: lambda r: 110 if r is long else 0
        self.assertEqual(self.hosts(Request("leg", 1000)), ["p"])
        # Without an estimate or a requested cap, the remaining output is unknown.
        self.scheduler.output_estimator = lambda: lambda r: 0
        long.wanted_len = 0
        self.assertEqual(self.hosts(Request("leg", 1000)), ["p"])
        # A resident at its requested cap has no output left to slow.
        long.wanted_len = long.output_len
        self.assertEqual(self.hosts(Request("leg", 1000)), ["p", "d"])

    def test_the_decode_leg_of_a_decode_engine_prefill_avoids_its_producer(self):
        self.backlog()
        producer = self.scheduler.schedule(Request("leg", 1000))
        self.assertEqual(producer.iid, "d")
        decode = Request("leg", 1000, phase=Phase.DECODE, prefill_instance="d")
        self.assertEqual(self.scheduler.schedule(decode).iid, "q")


class DecodeEnginePrefillJournalTests(HttpHarness):
    async def test_the_journal_records_the_role_of_the_engine_that_ran_prefill(self):
        self.cfg.serving = ServingPolicy(prefill_on_decode=True)
        ProfileStore(self.cfg.profiles_path).put(profile("e3", **ENVELOPE))
        client = self.client()
        self.assertEqual((await self.post(client)).status_code, 200)
        # A prefill engine held for a switch leaves the leg to the decode engine.
        self.router.scheduler.hold_for_switch("e0")
        self.assertEqual((await self.post(client)).status_code, 200)
        rows = self.terminal_rows()
        self.assertEqual(
            [(row["prefill_iid"], row["prefill_role"]) for row in rows],
            [("e0", "prefill"), ("e3", "decode")],
        )
        self.assert_released()


class DecodeEnginePrefillRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_queued_prefill_leg_waits_for_a_decode_engine_only_with_the_setting(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                folder = tempfile.TemporaryDirectory()
                self.addCleanup(folder.cleanup)
                cfg = fleet(Path(folder.name))
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
