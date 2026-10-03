"""Recovery probes must keep engines excluded when loaded measurements are stale."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from narwhal.engines.attestation import EngineIdentity
from narwhal.engines.client import InferenceProbe, ProbeLeg
from narwhal.profiling.generation import identity_generation
from narwhal.runtime import state
from narwhal.runtime.lease import FileLease
from narwhal.runtime.lifecycle.identity import allow_profile_recovery
from narwhal.runtime.monitoring import readmit, sweep_liveness
from narwhal.runtime.standby import ready, standby_loop
from narwhal.serving.app import create_app
from tests.fixtures import bind_identity_profiles, fleet


class ProfileRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """Use actual identity responses and scheduler state for contract-free recovery."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.cfg = fleet(self.root)
        self.cfg.engine_contract = None
        self.cfg.state_path = self.root / "handoff.json"
        self.router = create_app(self.cfg).state.router
        self.addAsyncCleanup(self.router.engines.aclose)
        self.starts = bind_identity_profiles(self.router)
        self.router.engines.healthy = AsyncMock(return_value=True)
        self.router.engines.probe_inference = AsyncMock(
            return_value=InferenceProbe(ProbeLeg(), ProbeLeg())
        )

    def refresh_profile(self, iid):
        generation = identity_generation(EngineIdentity("fixture", self.starts[iid]))
        self.router.profiles.put(
            replace(self.router.profiles.get(iid), generation_digest=generation.digest)
        )

    async def test_automatic_health_recovery_requires_current_measured_identity(self):
        self.router.scheduler.eject("e0")
        self.starts["e0"] += 1
        self.assertEqual(await readmit(self.router, 0), [])
        self.assertIn("e0", self.router.scheduler.ejected)
        event = self.router.lifecycle.events[-1]
        self.assertEqual(event["action"], "profile_recovery_blocked")
        self.assertIn("e0 profile generation differs", event["error"])
        self.refresh_profile("e0")
        self.assertEqual(await readmit(self.router, 0), ["e0"])
        self.assertNotIn("e0", self.router.scheduler.ejected)

    async def test_missing_profile_or_binding_cannot_recover(self):
        original = self.router.profiles.get("e0")
        for candidate, message in (
            (None, "has no loaded profile"),
            (replace(original, generation_digest=None), "has no generation evidence"),
        ):
            with self.subTest(message=message):
                if candidate is None:
                    del self.router.profiles._by_id["e0"]
                else:
                    self.router.profiles.put(candidate)
                self.router.scheduler.eject("e0")
                self.assertEqual(await readmit(self.router, 0), [])
                self.assertIn(message, self.router.lifecycle.events[-1]["error"])
        self.router.profiles.put(original)
        self.assertEqual(await readmit(self.router, 0), ["e0"])

    async def test_successful_inference_probe_does_not_bypass_binding(self):
        self.router.scheduler.eject("e3")
        self.router.scheduler.inference_suspects.add("e3")
        self.router.verifier.sources["e3"] = {"e0"}
        self.starts["e3"] += 1
        self.assertEqual(await readmit(self.router, 0), [])
        self.assertIn("e3", self.router.scheduler.ejected)
        self.assertIn("e3", self.router.scheduler.inference_suspects)
        self.assertEqual(self.router.verifier.sources["e3"], {"e0"})
        self.refresh_profile("e3")
        self.assertEqual(await readmit(self.router, 0), ["e3"])
        self.assertNotIn("e3", self.router.scheduler.inference_suspects)
        self.assertNotIn("e3", self.router.verifier.sources)

    async def test_health_verification_and_liveness_cannot_clear_stale_quarantine(self):
        self.starts["e0"] += 1
        for action in (self.router.verifier._verify_health, sweep_liveness):
            with self.subTest(action=action.__name__):
                self.router.scheduler.ejected.clear()
                self.router.scheduler.quarantined["e0"] = self.router._clock() + 100
                if action == sweep_liveness:
                    await action(self.router)
                else:
                    await action("e0", self.cfg.engines[0].url)
                self.assertIn("e0", self.router.scheduler.ejected)
                self.assertNotIn(
                    "e0", [inst.iid for inst in self.router.scheduler.live_instances()]
                )

    async def test_recovery_preserves_operator_holds_and_control_fencing(self):
        for hold in ("draining", "wave", "standby"):
            with self.subTest(hold=hold):
                self.router.scheduler.eject("e0")
                self.router.scheduler.draining.clear()
                if hold == "draining":
                    self.router.scheduler.draining.add("e0")
                self.router.lifecycle_blocked = "wave hold" if hold == "wave" else ""
                self.router.standby = hold == "standby"
                with patch(
                    "narwhal.runtime.lifecycle.identity.read_generation", new=AsyncMock()
                ) as read:
                    self.assertFalse(await allow_profile_recovery(self.router, "e0"))
                read.assert_not_awaited()
                self.assertIn("e0", self.router.scheduler.ejected)

    async def test_fencing_during_identity_fetch_prevents_recovery(self):
        self.router.scheduler.eject("e0")

        async def fenced(*args, **kwargs):
            self.router.standby = True
            return identity_generation(EngineIdentity("fixture", self.starts["e0"]))

        with patch("narwhal.runtime.lifecycle.identity.read_generation", side_effect=fenced):
            self.assertEqual(await readmit(self.router, 0), [])
        self.assertIn("e0", self.router.scheduler.ejected)

    async def test_ordinary_liveness_does_not_add_identity_requests(self):
        with patch("narwhal.runtime.lifecycle.identity.read_generation", new=AsyncMock()) as read:
            self.assertEqual(await sweep_liveness(self.router), [])
        read.assert_not_awaited()

    async def test_ejection_during_identity_fetch_preserves_the_new_wave_hold(self):
        """The synchronous request-failure callback can install a hold while a probe awaits."""
        self.cfg.engine_restart_policy = "whole_wave"
        self.router.scheduler.on_eject = lambda iid: self.router.lifecycle.require_restart_wave(
            "engine excluded during recovery probe"
        )

        async def excluded(*args, **kwargs):
            self.router.scheduler.eject("e0")
            return identity_generation(EngineIdentity("fixture", self.starts["e0"]))

        with patch("narwhal.runtime.lifecycle.identity.read_generation", side_effect=excluded):
            await self.router.verifier._verify_health("e0", self.cfg.engines[0].url)
        self.assertIn("e0", self.router.scheduler.ejected)
        self.assertEqual(self.router.scheduler.draining, {"e0", "e3"})
        self.assertTrue(self.router.lifecycle.wave_id)
        self.assertFalse(ready(self.router))

    async def test_standby_takeover_excludes_stale_loaded_profiles_before_readiness(self):
        state.write(self.cfg.state_path, state.snapshot(self.router))
        self.router.standby = True
        lease = FileLease(self.root / "lease.json", "standby", 30)
        self.addCleanup(lease.release)
        self.router.lease = lease
        self.starts["e0"] += 1
        observed_readiness = []
        original_transport = self.router.lifecycle_transport

        async def identity(request):
            if not observed_readiness:
                await sweep_liveness(self.router)
            observed_readiness.append(ready(self.router))
            return await original_transport.handle_async_request(request)

        self.router.lifecycle_transport = httpx.MockTransport(identity)
        await standby_loop(
            self.router,
            "http://primary",
            lease,
            probe_interval_s=0.001,
            takeover_after=1,
            transport=httpx.MockTransport(lambda request: httpx.Response(503)),
        )
        self.assertTrue(observed_readiness)
        self.assertFalse(any(observed_readiness))
        self.assertFalse(self.router.standby)
        self.assertTrue(ready(self.router))
        self.assertEqual(set(self.router.scheduler.ejected), {"e0"})
