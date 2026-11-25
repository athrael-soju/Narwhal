"""Preserve primary handoffs when a non-owning router shuts down."""

import asyncio
import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from narwhal.config import SLO, EngineSpec, FleetConfig
from narwhal.contracts import ContractVersionError
from narwhal.profiling.generation import GenerationEvidence
from narwhal.profiling.model import Profile
from narwhal.profiling.store import ProfileStore
from narwhal.runtime import state
from narwhal.runtime.lease import FileLease
from narwhal.runtime.standby import lease_role, standby_loop
from narwhal.serving.app import create_app
from narwhal.types import Role


class FleetCase(unittest.IsolatedAsyncioTestCase):
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
        generation = "sha256:" + "a" * 64
        generation_reader = patch(
            "narwhal.profiling.membership.read_generation",
            new=AsyncMock(return_value=GenerationEvidence(generation, {"engine": {}}, 100.0)),
        )
        generation_reader.start()
        self.addCleanup(generation_reader.stop)
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
                    generation_digest=generation,
                )
            )


class StandbyRoleTests(FleetCase):
    async def test_a_standby_reports_standby_again_once_shadowing_resumes(self):
        """A takeover attempt that found no fresh handoff fences the standby until the
        primary answers again, and no longer."""
        owner = FileLease(self.root / "lease.json", "primary", 30)
        self.assertTrue(owner.claim())
        self.addCleanup(owner.release)
        primary = create_app(self.cfg).state.router
        self.addAsyncCleanup(primary.engines.aclose)
        primary.lease_epoch, primary.lease_holder = owner.epoch, owner.holder
        document = state.snapshot(primary)
        router = create_app(self.cfg).state.router
        self.addAsyncCleanup(router.engines.aclose)
        router.standby = True
        router.lease = FileLease(self.root / "lease.json", "standby", 30)
        answering = False

        def transport(request):
            if not answering:
                return httpx.Response(503, json={"control_ready": False})
            if request.url.path == "/ready":
                return httpx.Response(200, json={"ready": True})
            return httpx.Response(200, json=document)

        async def until(condition):
            for _ in range(2000):
                if condition():
                    return
                await asyncio.sleep(0.005)
            self.fail("the standby loop did not reach the expected state")

        loop = asyncio.create_task(
            standby_loop(
                router,
                "http://primary",
                router.lease,
                probe_interval_s=0.005,
                takeover_after=2,
                transport=httpx.MockTransport(transport),
            )
        )
        try:
            await until(lambda: router.failover_blocked == "no fresh handoff")
            self.assertEqual(lease_role(router), "fenced")
            self.assertFalse(router.primary_seen)
            answering = True
            await until(lambda: router.failover_blocked == "")
            self.assertEqual(lease_role(router), "standby")
            self.assertTrue(router.standby)
            # A probe that finds the primary controlling the fleet marks it live, and the next
            # probe that does not, such as after the primary is killed, clears the mark.
            self.assertTrue(router.primary_seen)
            answering = False
            await until(lambda: not router.primary_seen)
        finally:
            loop.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop

    async def test_a_loop_that_ends_on_an_unsupported_handoff_stops_vouching_for_the_primary(self):
        owner = FileLease(self.root / "lease.json", "primary", 30)
        self.assertTrue(owner.claim())
        self.addCleanup(owner.release)
        primary = create_app(self.cfg).state.router
        self.addAsyncCleanup(primary.engines.aclose)
        primary.lease_epoch, primary.lease_holder = owner.epoch, owner.holder
        document = state.snapshot(primary)
        router = create_app(self.cfg).state.router
        self.addAsyncCleanup(router.engines.aclose)
        router.standby = True
        router.lease = FileLease(self.root / "lease.json", "standby", 30)
        upgraded = False

        def transport(request):
            if request.url.path == "/ready":
                return httpx.Response(200, json={"ready": True})
            if upgraded:
                return httpx.Response(200, json={**document, "schema_version": 99})
            return httpx.Response(200, json=document)

        loop = asyncio.create_task(
            standby_loop(
                router,
                "http://primary",
                router.lease,
                probe_interval_s=0.005,
                transport=httpx.MockTransport(transport),
            )
        )
        for _ in range(2000):
            if router.primary_seen:
                break
            await asyncio.sleep(0.005)
        self.assertTrue(router.primary_seen)
        upgraded = True
        with self.assertRaises(ContractVersionError):
            await asyncio.wait_for(loop, timeout=10)
        self.assertFalse(router.primary_seen)


class ShutdownHandoffTests(FleetCase):
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


class YieldControlTests(FleetCase):
    async def test_a_lease_holder_hands_off_as_shutdown_starts(self):
        lease = FileLease(self.root / "lease.json", "primary", 30)
        app = create_app(self.cfg, lease=lease)
        async with app.router.lifespan_context(app):
            router = app.state.router
            self.assertTrue(lease.owned)
            router.served = 5
            app.state.yield_control()
            self.assertTrue(router.standby)
            self.assertEqual(router.failover_blocked, "shutting down")
            self.assertEqual(state.load(self.cfg.state_path)["counters"]["served"], 5)
            # The standby claims the lease while this process still drains.
            standby = FileLease(self.root / "lease.json", "standby", 30)
            self.assertTrue(standby.claim())
            self.addCleanup(standby.release)
            # Requests that finish during the drain do not overwrite the handoff.
            router.served = 6
        self.assertEqual(state.load(self.cfg.state_path)["counters"]["served"], 5)

    async def test_a_router_without_a_lease_keeps_control_through_the_drain(self):
        app = create_app(self.cfg)
        async with app.router.lifespan_context(app):
            app.state.yield_control()
            self.assertFalse(app.state.router.standby)
            app.state.router.served = 3
        self.assertEqual(state.load(self.cfg.state_path)["counters"]["served"], 3)


class YieldingServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_server_stops_accepting_before_it_yields_control(self):
        import uvicorn

        from narwhal.cli import YieldingServer

        order = []

        class App:
            pass

        app = App()
        app.state = type(
            "State", (), {"yield_control": staticmethod(lambda: order.append("yield"))}
        )()
        server = YieldingServer(uvicorn.Config(app, timeout_graceful_shutdown=0))

        class Listener:
            def close(self):
                order.append("close")

        class Connection:
            def shutdown(self):
                order.append("shutdown")

        server.servers = [Listener()]
        server.server_state.connections.add(Connection())
        server.lifespan = type("Lifespan", (), {"shutdown": AsyncMock()})()
        await server.shutdown()
        # Requests that reach the router before the yield are admitted under the lease.
        self.assertEqual(order[:3], ["close", "shutdown", "yield"])
