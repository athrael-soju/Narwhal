import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from narwhal.diagnostics.check.profiles import gate_profile, gate_profile_generation
from narwhal.diagnostics.check.report import Report
from narwhal.profiling.generation import profile_generation_problems
from narwhal.profiling.model import Profile, nominal_profile
from narwhal.profiling.store import ProfileStore
from narwhal.runtime.lifecycle.identity import allow_profile_recovery
from narwhal.serving.app import create_app
from tests.fixtures import fleet, profile

DIGEST = "sha256:" + "a" * 64
REFUSED = "e0 has a nominal profile; engine_contract requires a measured one"


def nominal(iid: str, **changes) -> Profile:
    figures = {
        "prefill_token_s": 0.0002,
        "token_interval_s": 0.01,
        "kv_capacity_tokens": 4096,
        "max_requests": 4,
    }
    return nominal_profile(iid, **{**figures, **changes})


def unreachable(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"a nominal profile read {request.url}")


class NominalProfileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.cfg = fleet(self.root)
        self.store = ProfileStore(self.cfg.profiles_path)
        for spec in self.cfg.engines:
            self.store.put(nominal(spec.iid))
        self.transport = httpx.MockTransport(unreachable)

    def test_the_stated_figures_set_the_prices_and_limits(self):
        profile = nominal("e0")
        self.assertAlmostEqual(profile.prefill_time(1000), 0.2)
        self.assertEqual(profile.token_interval(4096, 4), 0.01)
        self.assertEqual(profile.max_tokens(0.05, 4), 4096)
        self.assertEqual(profile.max_tokens(0.05, 5), 0)
        self.assertEqual(profile.max_tokens(0.005), 0)
        self.assertEqual(profile.decode_request_limit(1024), 4)
        self.assertEqual(profile.decode_request_limit(2048), 2)
        self.assertGreater(profile.decode_rps(0.05, 1024, 64), 0)
        for figure in ("prefill_token_s", "token_interval_s"):
            with self.assertRaisesRegex(ValueError, f"profile e0: {figure} must be positive"):
                nominal("e0", **{figure: 0})

    def test_the_store_keeps_the_flag_and_rejects_a_bound_or_malformed_one(self):
        self.store.put(profile("e3"))
        loaded = ProfileStore(self.cfg.profiles_path)
        self.assertEqual(loaded.get("e0"), nominal("e0"))
        self.assertEqual(loaded.get("e3"), profile("e3"))
        rows = json.loads(self.cfg.profiles_path.read_text())["profiles"]
        self.assertEqual(["nominal" in row for row in rows], [True, False])
        with self.assertRaisesRegex(ValueError, "a nominal profile carries no generation_digest"):
            replace(nominal("e0"), generation_digest=DIGEST)
        document = json.loads(self.cfg.profiles_path.read_text())
        document["profiles"][0]["nominal"] = "yes"
        self.cfg.profiles_path.write_text(json.dumps(document))
        with self.assertRaisesRegex(ValueError, "profile e0: nominal must be true or false"):
            ProfileStore(self.cfg.profiles_path)

    def test_a_generation_check_passes_a_nominal_profile_and_still_needs_a_profile(self):
        self.assertEqual(profile_generation_problems(self.store, "e0", DIGEST, contract=False), [])
        self.assertEqual(
            profile_generation_problems(self.store, "e0", DIGEST, contract=True), [REFUSED]
        )
        self.assertEqual(
            profile_generation_problems(self.store, "absent", DIGEST, contract=False),
            ["absent has no loaded profile; reprofile before admission"],
        )

    async def test_a_contract_free_router_starts_and_recovers_without_reading_the_engine(self):
        self.cfg.engine_contract = None
        self.cfg.liveness_every = 0
        self.cfg.state_path = self.root / "handoff.json"
        app = create_app(self.cfg, lifecycle_transport=self.transport)
        router = app.state.router
        with patch("narwhal.serving.app.monitor_loop", new=AsyncMock()):
            async with app.router.lifespan_context(app):
                self.assertTrue(await allow_profile_recovery(router, "e0"))
        self.store.put(nominal("e3", max_requests=1))
        report = Report()
        store = gate_profile(self.cfg, report)
        live = {spec.iid for spec in self.cfg.engines}
        self.assertEqual(
            await gate_profile_generation(self.cfg, store, live, report, self.transport), set()
        )
        self.assertEqual(report.failed, [])

    async def test_an_engine_contract_refuses_a_nominal_profile(self):
        app = create_app(self.cfg, lifecycle_transport=self.transport)
        self.addAsyncCleanup(app.state.router.engines.aclose)
        with self.assertRaisesRegex(RuntimeError, REFUSED):
            async with app.router.lifespan_context(app):
                pass
        report = Report()
        gate_profile(self.cfg, report)
        self.assertEqual(report.failed, [REFUSED, REFUSED.replace("e0", "e3")])
