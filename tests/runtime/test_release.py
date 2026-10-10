import asyncio
import os
import runpy
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from narwhal.backends import load as load_backend
from narwhal.config import FleetConfig
from narwhal.engines.client import InferenceProbe, ProbeLeg
from narwhal.runtime.lifecycle.records import DrainRecord
from narwhal.runtime.monitoring import monitor_once
from narwhal.runtime.release import (
    PeerRelease,
    release_peers,
    release_round,
    released_engines,
)
from narwhal.serving.app import create_app
from tests.fixtures import ROOT, profile

FABRIC = load_backend("vllm").fabric
RELEASE_AFTER_S = FABRIC.release_after_s
RETRY_AFTER_S = FABRIC.release_retry_s


class PeerReleaseScheduleTests(unittest.TestCase):
    def test_rounds_follow_ejection_until_readmission(self):
        now = [100.0]
        release = PeerRelease(lambda: now[0], FABRIC)
        release.track({"e5": "ejected"})
        self.assertEqual(release.due(), [])
        self.assertEqual(release.snapshot(), {"e5": {"rounds": 0, "next_round_s": 65.0}})
        now[0] += RELEASE_AFTER_S[0] - 0.5
        self.assertEqual(release.due(), [])
        now[0] += 0.5
        self.assertEqual(release.due(), ["e5"])
        self.assertEqual(release.due(), [])
        self.assertEqual(release.snapshot()["e5"]["rounds"], 1)
        now[0] = 100.0 + RELEASE_AFTER_S[-1]
        self.assertEqual(release.due(), ["e5"])
        self.assertEqual(release.due(), ["e5"])
        for _ in RELEASE_AFTER_S:
            release.due()
        self.assertEqual(
            release.snapshot(), {"e5": {"rounds": len(RELEASE_AFTER_S), "next_round_s": None}}
        )
        release.track({})
        self.assertEqual(release.due(), [])
        self.assertEqual(release.snapshot(), {})
        release.track({"e5": "ejected"})
        self.assertEqual(release.snapshot()["e5"]["rounds"], 0)

    def test_a_state_change_restarts_the_schedule(self):
        now = [100.0]
        release = PeerRelease(lambda: now[0], FABRIC)
        release.track({"e5": "ejected"})
        now[0] += RELEASE_AFTER_S[0]
        self.assertEqual(release.due(), ["e5"])
        now[0] += 30.0
        release.track({"e5": "ejected"})
        self.assertEqual(release.snapshot()["e5"]["rounds"], 1)
        release.track({"e5": "blocked"})
        self.assertEqual(release.snapshot(), {"e5": {"rounds": 0, "next_round_s": 65.0}})

    def test_first_round_follows_the_launcher_engine_ttl(self):
        from narwhal.backends.vllm.plan import ENGINE_TTL_S

        self.assertGreater(RELEASE_AFTER_S[0], ENGINE_TTL_S)
        self.assertGreater(RELEASE_AFTER_S[-1], 3600.0)
        self.assertEqual(list(RELEASE_AFTER_S), sorted(RELEASE_AFTER_S))

    def test_the_capture_hook_waits_through_two_release_rounds(self):
        with patch.dict(os.environ):
            os.environ.pop("NARWHAL_CAPTURE_CACHE", None)
            hook = runpy.run_path(str(ROOT / "src/narwhal/backends/vllm/cache_capture_hook.py"))
        self.assertGreater(hook["PEER_RELEASE_WAIT_S"], RELEASE_AFTER_S[1])


class PeerReleaseRoundTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        cfg = FleetConfig.load(ROOT / "tests/data/fleet.json")
        cfg.engine_contract = None
        cfg.liveness_every = 0
        cfg.state_path = root / "handoff.json"
        cfg.profiles_path = root / "profiles.json"
        self.cfg = cfg
        self.router = create_app(cfg).state.router
        for spec in cfg.engines:
            self.router.profiles.put(profile(spec.iid))
        self.addAsyncCleanup(self.router.engines.aclose)
        self.now = [1000.0]
        self.router.peer_release = PeerRelease(lambda: self.now[0], FABRIC)
        self.urls = {spec.url: spec.iid for spec in cfg.engines}
        self.calls: list[tuple[str, str]] = []

        async def probe(url, *, prefill_url=None, deadline_s=None, producer=None):
            self.calls.append((self.urls[prefill_url], self.urls[url]))
            return InferenceProbe(prefill=ProbeLeg(), decode=ProbeLeg())

        self.probe = AsyncMock(side_effect=probe)
        self.router.engines.probe_inference = self.probe

    async def test_round_probes_every_live_consumer_through_another_producer(self):
        self.router.scheduler.eject("e5", "liveness")
        outcomes = await release_round(self.router, ["e5"])
        self.assertEqual(outcomes, dict.fromkeys(["e0", "e1", "e2", "e3", "e4"], "sent"))
        self.assertEqual(
            sorted(consumer for _, consumer in self.calls), ["e0", "e1", "e2", "e3", "e4"]
        )
        self.assertTrue(all(producer != consumer for producer, consumer in self.calls))
        self.assertNotIn("e5", {iid for call in self.calls for iid in call})

    async def test_role_pins_limit_producers_and_consumers(self):
        engines = self.cfg.engines
        self.cfg.engines = [replace(spec, pin=spec.iid in ("e0", "e3")) for spec in engines]
        self.router.scheduler.eject("e5", "liveness")
        await release_round(self.router, ["e5"])
        consumers = sorted(consumer for _, consumer in self.calls)
        producers = {producer for producer, _ in self.calls}
        self.assertEqual(consumers, ["e1", "e2", "e3", "e4"])
        self.assertNotIn("e3", producers)

    async def test_failed_producer_and_lone_consumer_are_reported(self):
        async def failing(url, *, prefill_url=None, deadline_s=None, producer=None):
            return InferenceProbe(prefill=ProbeLeg(failed="connection"), decode=ProbeLeg())

        self.router.engines.probe_inference = AsyncMock(side_effect=failing)
        for iid in ("e2", "e3", "e4", "e5"):
            self.router.scheduler.eject(iid, "liveness")
        outcomes = await release_round(self.router, ["e5"])
        self.assertEqual(outcomes["e0"], "producer e1 connection")
        self.router.scheduler.eject("e1", "liveness")
        with self.assertLogs("narwhal.peer_release", level="WARNING") as logs:
            self.assertEqual(await release_round(self.router, ["e5"]), {"e0": "no producer"})
        self.assertIn("peer release for e5: e0 no producer", logs.output[0])
        self.assertIn("whole-wave restart", logs.output[0])

    async def test_release_starts_one_round_when_due_and_skips_whole_wave(self):
        self.router.scheduler.eject("e5", "liveness")
        release_peers(self.router)
        self.assertEqual(self.router.peer_release.tasks, set())
        self.now[0] += RELEASE_AFTER_S[0]
        release_peers(self.router)
        release_peers(self.router)
        self.assertEqual(len(self.router.peer_release.tasks), 1)
        await asyncio.gather(*self.router.peer_release.tasks)
        self.assertEqual(self.probe.await_count, 5)
        self.assertEqual(self.router.state()["peer_release"]["e5"]["rounds"], 1)
        self.router.cfg.engine_restart_policy = "whole_wave"
        self.now[0] += RELEASE_AFTER_S[1]
        release_peers(self.router)
        self.assertEqual(self.router.peer_release.tasks, set())

    async def test_missed_consumers_retry_before_the_next_round(self):
        missed = {"e1", "e3"}

        async def probe(url, *, prefill_url=None, deadline_s=None, producer=None):
            consumer = self.urls[url]
            self.calls.append((self.urls[prefill_url], consumer))
            if consumer in missed:
                missed.discard(consumer)
                leg = ProbeLeg(inconclusive=True) if consumer == "e1" else ProbeLeg()
                decode = ProbeLeg(failed="connection") if consumer == "e3" else ProbeLeg()
                return InferenceProbe(prefill=leg, decode=decode)
            return InferenceProbe(prefill=ProbeLeg(), decode=ProbeLeg())

        self.router.engines.probe_inference = AsyncMock(side_effect=probe)
        self.router.scheduler.eject("e5", "liveness")
        release_peers(self.router)
        self.now[0] += RELEASE_AFTER_S[0]
        with self.assertLogs("narwhal.peer_release", level="INFO") as logs:
            release_peers(self.router)
            await asyncio.gather(*self.router.peer_release.tasks)
        self.assertIn("e1 producer", logs.output[0])
        self.assertIn("pool timeout", logs.output[0])
        self.assertIn("e3 consumer connection", logs.output[0])
        self.assertEqual(self.router.peer_release.missed, {"e1", "e3"})
        self.calls.clear()
        release_peers(self.router)
        self.assertEqual(self.router.peer_release.tasks, set())
        self.now[0] += RETRY_AFTER_S
        release_peers(self.router)
        await asyncio.gather(*self.router.peer_release.tasks)
        self.assertEqual(sorted(consumer for _, consumer in self.calls), ["e1", "e3"])
        self.assertEqual(self.router.peer_release.missed, set())
        self.assertEqual(self.router.state()["peer_release"]["e5"]["rounds"], 1)
        self.calls.clear()
        self.now[0] += RETRY_AFTER_S
        release_peers(self.router)
        self.assertEqual(self.router.peer_release.tasks, set())

    async def test_a_retry_runs_once_through_the_next_producer(self):
        async def failing(url, *, prefill_url=None, deadline_s=None, producer=None):
            self.calls.append((self.urls[prefill_url], self.urls[url]))
            return InferenceProbe(prefill=ProbeLeg(failed="connection"), decode=ProbeLeg())

        self.router.engines.probe_inference = AsyncMock(side_effect=failing)
        self.router.scheduler.eject("e5", "liveness")
        release_peers(self.router)
        self.now[0] += RELEASE_AFTER_S[0]
        release_peers(self.router)
        await asyncio.gather(*self.router.peer_release.tasks)
        first = {consumer: producer for producer, consumer in self.calls}
        self.calls.clear()
        self.now[0] += RETRY_AFTER_S
        release_peers(self.router)
        await asyncio.gather(*self.router.peer_release.tasks)
        retried = {consumer: producer for producer, consumer in self.calls}
        self.assertEqual(set(retried), set(first))
        self.assertTrue(all(retried[iid] != first[iid] for iid in first))
        self.calls.clear()
        for _ in range(3):
            self.now[0] += RETRY_AFTER_S
            release_peers(self.router)
        self.assertEqual(self.router.peer_release.tasks, set())
        self.assertEqual(self.calls, [])

    async def test_a_consumer_without_another_producer_waits_for_the_next_round(self):
        for iid in ("e1", "e2", "e3", "e4"):
            self.router.scheduler.eject(iid, "liveness")
        self.router.scheduler.eject("e5", "liveness")
        release_peers(self.router)
        self.now[0] += RELEASE_AFTER_S[0]
        with self.assertLogs("narwhal.peer_release", level="WARNING"):
            release_peers(self.router)
            await asyncio.gather(*self.router.peer_release.tasks)
        self.assertEqual(self.router.peer_release.missed, set())
        self.now[0] += RETRY_AFTER_S
        release_peers(self.router)
        self.assertEqual(self.router.peer_release.tasks, set())
        self.assertEqual(self.probe.await_count, 0)

    async def test_drained_engines_join_release_rounds(self):
        records = self.router.lifecycle.records
        records["e4"] = DrainRecord("e4", "draining", 0.0, 300.0)
        records["e5"] = DrainRecord("e5", "drained", 0.0, 300.0)
        records["e3"] = DrainRecord("e3", "active", 0.0, 300.0)
        self.assertEqual(released_engines(self.router), {"e5": "drained"})
        self.router.scheduler.eject("e2", "liveness")
        self.assertEqual(released_engines(self.router), {"e2": "ejected", "e5": "drained"})
        records["e4"].state = "blocked"
        records["e2"] = DrainRecord("e2", "validating", 0.0, 300.0)
        self.assertEqual(
            released_engines(self.router), {"e2": "validating", "e4": "blocked", "e5": "drained"}
        )

    async def test_monitor_pass_runs_due_release_rounds(self):
        self.router.scheduler.eject("e5", "liveness")
        with patch("narwhal.runtime.monitoring.readmit", AsyncMock(return_value=[])):
            await monitor_once(self.router)
            self.now[0] += RELEASE_AFTER_S[0]
            await monitor_once(self.router)
        await asyncio.gather(*self.router.peer_release.tasks)
        self.assertEqual(self.probe.await_count, 5)


if __name__ == "__main__":
    unittest.main()
