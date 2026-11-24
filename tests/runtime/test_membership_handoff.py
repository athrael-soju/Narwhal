"""Carry membership through the handoff to a resumed router and a standby takeover."""

import copy
import json
from dataclasses import asdict, replace
from unittest.mock import patch

import httpx

from narwhal.config import EngineSpec
from narwhal.config.model import SharedDeviceAllocation
from narwhal.profiling import calibration
from narwhal.profiling.generation import GenerationEvidence
from narwhal.profiling.store import ProfileStore
from narwhal.runtime import state
from narwhal.runtime.lease import FileLease
from narwhal.runtime.standby import standby_loop
from narwhal.serving.app import create_app
from narwhal.types import Role
from tests.fixtures import calibration_document
from tests.runtime.test_standby_shutdown import FleetCase

JOINED = EngineSpec("x", "http://joined", Role.PREFILL)
DIGEST = "sha256:" + "a" * 64


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
            {"generation": 1, "engines": [{"iid": "d", "url": "http://a"}] * 2},
        ):
            doc = {**copy.deepcopy(self.doc), "membership": membership}
            with self.subTest(membership=membership), self.assertRaises(ValueError):
                state.validate(doc)

    def test_keys_a_later_release_adds_are_ignored(self):
        doc = copy.deepcopy(self.doc)
        doc["membership"]["epoch_hint"] = 3
        doc["membership"]["engines"][0]["weight"] = 2
        state.validate(doc)
        self.assertEqual(state.membership_of(doc).ids, ["d", "x"])

    async def test_an_unchanged_membership_keeps_the_fleet_file_entries(self):
        router = create_app(self.cfg).state.router
        self.addAsyncCleanup(router.engines.aclose)
        doc = state.snapshot(router)
        self.assertEqual(doc["membership"]["generation"], 0)
        # The operator edits the fleet file and restarts with --resume.
        edited = replace(
            self.cfg,
            engines=[
                replace(self.cfg.engines[0], pin=True),
                replace(self.cfg.engines[1], url="http://decode-new"),
            ],
        )
        resumed = create_app(edited).state.router
        self.addAsyncCleanup(resumed.engines.aclose)
        report = state.apply(resumed, doc)
        self.assertTrue(report.applied, report.why)
        self.assertFalse(report.membership_adopted)
        self.assertEqual(resumed.membership.spec("d").url, "http://decode-new")
        self.assertIn("p", resumed.scheduler.pinned)

    async def test_a_changed_membership_keeps_this_routers_entry_for_known_engines(self):
        edited = replace(
            self.cfg, engines=[self.cfg.engines[0], replace(self.cfg.engines[1], url="http://d2")]
        )
        router = create_app(edited).state.router
        self.addAsyncCleanup(router.engines.aclose)
        with self.assertLogs("narwhal.state", level="WARNING") as logs:
            report = state.apply(router, self.doc)
        self.assertTrue(report.applied, report.why)
        self.assertTrue(report.membership_adopted)
        self.assertEqual(router.membership.spec("d").url, "http://d2")
        self.assertEqual(router.membership.spec("x").url, JOINED.url)
        self.assertIn("d keeps this router's engine entry", logs.output[0])

    async def test_a_shared_device_membership_change_refuses_the_handoff(self):
        allocation = SharedDeviceAllocation("g", "GPU-x", 0.5, 0.4)
        doc = copy.deepcopy(self.doc)
        doc["membership"]["engines"][1]["shared_device"] = asdict(allocation)
        router = create_app(self.cfg).state.router
        self.addAsyncCleanup(router.engines.aclose)
        report = state.apply(router, doc)
        self.assertFalse(report.applied)
        self.assertIn("changes shared-device engines x", report.why)

    async def take_over(self, covered, down=()):
        """Take over with a calibration of `covered`, while engines in `down` do not answer."""
        path = self.root / "calibration.json"
        generations = dict.fromkeys(covered, DIGEST)
        specs = {spec.iid: spec for spec in (*self.cfg.engines, JOINED)}
        fleet = replace(self.cfg, engines=[specs[iid] for iid in covered])
        starts = dict.fromkeys(covered, 100.0)
        path.write_text(json.dumps(calibration_document(fleet, generations, starts)))
        cfg = replace(self.cfg, first_token_calibration_path=path)
        state.write(cfg.state_path, self.doc)
        router = create_app(cfg).state.router
        self.addAsyncCleanup(router.engines.aclose)
        router.standby = True
        lease = FileLease(self.root / "lease.json", "standby", 30)
        self.addCleanup(lease.release)
        router.lease = lease

        async def generation(cfg, spec, **kwargs):
            if spec.iid in down:
                raise httpx.ConnectError("down")
            return GenerationEvidence(DIGEST, {}, 100.0, DIGEST)

        with patch.object(calibration, "read_generation", generation):
            await standby_loop(
                router,
                "http://primary",
                lease,
                probe_interval_s=0.001,
                takeover_after=1,
                transport=httpx.MockTransport(lambda request: httpx.Response(503)),
            )
        return router

    async def test_a_takeover_refuses_a_calibration_that_misses_the_adopted_set(self):
        router = await self.take_over(["p", "d"])
        self.assertTrue(router.standby)
        self.assertIn("no samples for x", router.failover_blocked)

    async def test_a_takeover_does_not_read_an_ejected_engine_for_the_calibration(self):
        self.primary.scheduler.ejected["x"] = 0.0
        self.doc = state.snapshot(self.primary)
        router = await self.take_over(["d", "x"], down={"x"})
        self.assertFalse(router.standby, router.failover_blocked)
        self.assertEqual(router.first_token_calibration.status, "measured")

    async def test_a_resume_at_generation_zero_refuses_stale_profiles(self):
        store = ProfileStore(self.cfg.profiles_path)
        self.assertIsNotNone(store.get("x"))
        router = create_app(self.cfg).state.router
        self.addAsyncCleanup(router.engines.aclose)
        state.write(self.cfg.state_path, state.snapshot(router))
        app = create_app(replace(self.cfg, resume=True))
        self.addAsyncCleanup(app.state.router.engines.aclose)
        with self.assertRaisesRegex(RuntimeError, "unconfigured engines"):
            async with app.router.lifespan_context(app):
                pass

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
