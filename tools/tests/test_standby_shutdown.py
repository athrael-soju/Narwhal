"""Preserve primary handoffs when a non-owning router shuts down."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from narwhal.config import SLO, EngineSpec, FleetConfig
from narwhal.profiling.model import Profile
from narwhal.profiling.store import ProfileStore
from narwhal.runtime import state
from narwhal.runtime.lease import FileLease
from narwhal.serving.app import create_app
from narwhal.types import Role


class ShutdownHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.cfg = FleetConfig(
            model="stub",
            slo=SLO(ttft_s=1.0, tpot_s=0.02),
            engines=[
                EngineSpec("p", "http://prefill", Role.PREFILL),
                EngineSpec("d", "http://decode", Role.DECODE),
            ],
            profiles_path=self.root / "profiles.json",
            state_path=self.root / "handoff.json",
            monitor_interval_s=60,
        )
        profiles = ProfileStore(self.cfg.profiles_path)
        for iid in ("p", "d"):
            profiles.put(
                Profile(
                    iid,
                    0.0,
                    0.001,
                    0.01,
                    0.000001,
                    0.01,
                    decode_min_requests=1,
                    decode_max_requests=4,
                    decode_min_kv_tokens=1,
                    decode_max_kv_tokens=1024,
                    decode_fit_mape=0.0,
                    decode_cv_mape=0.0,
                )
            )

    async def test_standby_shutdown_preserves_polled_primary_handoff(self):
        owner = FileLease(self.root / "lease.json", "primary", 30)
        self.assertTrue(owner.claim())
        self.addCleanup(owner.release)
        primary = create_app(self.cfg).state.router
        self.addAsyncCleanup(primary.engines.aclose)
        primary.served = 9
        primary.lease_epoch, primary.lease_holder = owner.epoch, owner.holder
        document = state.snapshot(primary)

        def transport(request):
            if request.url.path == "/ready":
                return httpx.Response(200, json={"ready": True})
            self.assertEqual(request.url.path, "/narwhal/handoff")
            return httpx.Response(200, json=document)

        app = create_app(
            self.cfg,
            standby_of="http://primary",
            standby_probe_interval_s=0.01,
            standby_transport=httpx.MockTransport(transport),
            lease=FileLease(self.root / "lease.json", "standby", 30),
        )
        async with app.router.lifespan_context(app):
            async with asyncio.timeout(2):
                while not self.cfg.state_path.exists():
                    await asyncio.sleep(0.01)
            before = self.cfg.state_path.read_bytes()
            self.assertEqual(json.loads(before)["counters"]["served"], 9)
            self.assertTrue(app.state.router.standby)
            self.assertEqual(app.state.router.served, 0)
        self.assertEqual(self.cfg.state_path.read_bytes(), before)

    async def test_active_shutdown_saves_latest_completed_work(self):
        app = create_app(self.cfg)
        async with app.router.lifespan_context(app):
            app.state.router.served = 11
            self.assertFalse(self.cfg.state_path.exists())
        self.assertEqual(state.load(self.cfg.state_path)["counters"]["served"], 11)

    async def test_fenced_shutdown_preserves_existing_handoff(self):
        app = create_app(self.cfg)
        async with app.router.lifespan_context(app):
            app.state.router.served = 7
            state.write(self.cfg.state_path, state.snapshot(app.state.router))
            before = self.cfg.state_path.read_bytes()
            app.state.router.standby = True
            app.state.router.served = 0
        self.assertEqual(self.cfg.state_path.read_bytes(), before)

    async def test_lease_expiry_during_cleanup_prevents_final_write(self):
        clock = [100.0]
        lease = FileLease(
            self.root / "lease.json",
            "primary",
            30,
            wall_clock=lambda: clock[0],
            monotonic_clock=lambda: clock[0],
        )
        app = create_app(self.cfg, lease=lease)
        original_close = app.state.router.engines.aclose

        async def expire_and_close():
            clock[0] += 60
            await original_close()

        with patch.object(app.state.router.engines, "aclose", side_effect=expire_and_close):
            async with app.router.lifespan_context(app):
                app.state.router.served = 7
                state.write(self.cfg.state_path, state.snapshot(app.state.router))
                before = self.cfg.state_path.read_bytes()
                app.state.router.served = 8
        self.assertEqual(self.cfg.state_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
