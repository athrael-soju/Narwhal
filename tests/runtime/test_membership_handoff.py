"""Carry membership through the handoff to a resumed router and a standby takeover."""

import copy
from dataclasses import replace

import httpx

from narwhal.config import EngineSpec
from narwhal.profiling.store import ProfileStore
from narwhal.runtime import state
from narwhal.runtime.lease import FileLease
from narwhal.runtime.standby import standby_loop
from narwhal.serving.app import create_app
from narwhal.types import Role
from tests.runtime.test_standby_shutdown import FleetCase

JOINED = EngineSpec("x", "http://joined", Role.PREFILL)


class MembershipHandoffTests(FleetCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        store = ProfileStore(self.cfg.profiles_path)
        store.put(replace(store.get("p"), iid="x"))
        self.primary = create_app(self.cfg).state.router
        self.addAsyncCleanup(self.primary.engines.aclose)
        # A join and a leave move the primary to generation 2 with engines d and x.
        self.primary.apply_membership(self.primary.membership.joined(JOINED))
        self.primary.apply_membership(self.primary.membership.left("p"))
        self.primary.served = 7
        self.doc = state.snapshot(self.primary)

    def assert_restored(self, router):
        self.assertEqual(router.membership.generation, 2)
        self.assertEqual(router.membership.specs, self.primary.membership.specs)
        self.assertEqual(list(router.monitor.instances), ["d", "x"])
        self.assertIs(router.monitor.instances["x"].role, Role.PREFILL)
        self.assertEqual(sorted(router.residency.views), ["d", "x"])
        self.assertIsNotNone(router.profiles.get("x"))
        self.assertEqual(router.served, 7)

    def test_the_handoff_carries_the_snapshot(self):
        self.assertEqual(self.doc["engines"], ["d", "x"])
        self.assertEqual(
            self.doc["membership"],
            {
                "generation": 2,
                "engines": [
                    {"iid": "d", "url": "http://decode", "role": "decode"},
                    {"iid": "x", "url": "http://joined", "role": "prefill"},
                ],
            },
        )
        state.validate(self.doc)
        for membership in (
            {"generation": -1, "engines": []},
            {"generation": True, "engines": []},
            {"generation": 1},
            {"generation": 1, "engines": [{"iid": "d"}]},
            {"generation": 1, "engines": [{"iid": "d", "url": "http://a", "color": 1}]},
            {"generation": 1, "engines": [{"iid": "d", "url": "http://a"}] * 2},
        ):
            doc = {**copy.deepcopy(self.doc), "membership": membership}
            with self.subTest(membership=membership), self.assertRaises(ValueError):
                state.validate(doc)

    async def test_a_resumed_restart_restores_the_snapshot(self):
        state.write(self.cfg.state_path, self.doc)
        # The fleet file still names the original engines.
        cfg = replace(self.cfg, resume=True)
        app = create_app(cfg)
        router = app.state.router
        self.assertEqual(router.membership.ids, ["p", "d"])
        async with app.router.lifespan_context(app):
            self.assert_restored(router)

    async def test_a_standby_takeover_restores_the_snapshot(self):
        state.write(self.cfg.state_path, self.doc)
        router = create_app(self.cfg).state.router
        self.addAsyncCleanup(router.engines.aclose)
        router.standby = True
        lease = FileLease(self.root / "lease.json", "standby", 30)
        self.addCleanup(lease.release)
        router.lease = lease
        await standby_loop(
            router,
            "http://primary",
            lease,
            probe_interval_s=0.001,
            takeover_after=1,
            transport=httpx.MockTransport(lambda request: httpx.Response(503)),
        )
        self.assertFalse(router.standby, router.failover_blocked)
        self.assert_restored(router)

    async def test_missing_profiles_refuse_the_handoff_with_a_reason(self):
        store = ProfileStore(self.cfg.profiles_path)
        store._by_id.pop("x")
        store.put(store.get("d"))
        router = create_app(self.cfg).state.router
        self.addAsyncCleanup(router.engines.aclose)
        report = state.apply(router, self.doc)
        self.assertFalse(report.applied)
        self.assertIn("handoff membership lacks profiles: no profile for x", report.why)
        self.assertEqual(router.membership.ids, ["p", "d"])
        self.assertEqual(router.served, 0)

    async def test_shared_device_and_generation_changes_refuse_the_handoff(self):
        router = create_app(self.cfg).state.router
        self.addAsyncCleanup(router.engines.aclose)
        router.apply_membership(router.membership.joined(JOINED))
        router.apply_membership(router.membership.left("p"))
        router.apply_membership(router.membership.left("x"))
        report = state.apply(router, self.doc)
        self.assertIn("generation 2 precedes this router's generation 3", report.why)
        doc = copy.deepcopy(self.doc)
        doc["engines"] = ["d", "x"]
        doc["membership"]["engines"][1]["iid"] = "y"
        report = state.apply(create_app(self.cfg).state.router, doc)
        self.assertIn("differs from its engine list", report.why)

    async def test_a_handoff_without_membership_keeps_the_engine_set_check(self):
        doc = copy.deepcopy(self.doc)
        del doc["membership"]
        router = create_app(self.cfg).state.router
        self.addAsyncCleanup(router.engines.aclose)
        report = state.apply(router, doc)
        self.assertFalse(report.applied)
        self.assertEqual(report.why, "handoff names ['d', 'x'], this fleet is ['d', 'p']")
        same = state.snapshot(router)
        del same["membership"]
        self.assertTrue(state.apply(router, same).applied)
        self.assertEqual(router.membership.generation, 0)
