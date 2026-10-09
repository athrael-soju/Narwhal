"""Check failed-engine verification producers, hold accounting and floor restoration."""

import asyncio
import json
import math
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from narwhal.engines.client import InferenceProbe, ProbeLeg
from narwhal.observability.metrics.render import render
from narwhal.runtime.monitoring import monitor_once, readmit
from narwhal.serving.app import create_app
from narwhal.serving.execution import _failed_leg
from narwhal.serving.outcomes import failure_reason
from narwhal.serving.schemas import StateOut
from narwhal.types import LEG_CONNECTION, Role
from tests.fixtures import bind_identity_profiles, fleet

PASSED = InferenceProbe(ProbeLeg(), ProbeLeg())
PRODUCER_DOWN = InferenceProbe(ProbeLeg(failed=LEG_CONNECTION), ProbeLeg(inconclusive=True))
POOL_TIMEOUT = InferenceProbe(ProbeLeg(inconclusive=True), ProbeLeg(inconclusive=True))
STREAM_FAILURE = httpx.ReadTimeout("decode")


class HeldEngineTests(unittest.IsolatedAsyncioTestCase):
    """A two-prefill, two-decode fleet with a controllable availability clock."""

    engines = ("e0", "e1", "e3", "e4")

    def specs(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        return fleet(Path(folder.name), engines=self.engines).engines

    def router(self, **changes):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        cfg = fleet(root, engines=self.engines)
        cfg.engine_contract = None
        cfg.eject_after = 1
        cfg.liveness_every = 0
        cfg.state_path = root / "handoff.json"
        for name, value in changes.items():
            setattr(cfg, name, value)
        router = create_app(cfg).state.router
        bind_identity_profiles(router)
        self.addAsyncCleanup(router.engines.aclose)
        self.now = [1000.0]

        def clock():
            return self.now[0]

        router._clock = clock
        router.scheduler._clock = clock
        router.scheduler.availability._clock = clock
        router.scheduler.prefill_floor._clock = clock
        router.journal.open()
        self.addCleanup(router.journal.close)
        self.urls = {spec.iid: spec.url for spec in cfg.engines}
        return router

    def events(self, router, *names):
        """Return journal event rows, optionally only the named events."""
        rows = [json.loads(line) for line in router.journal.path.read_text().splitlines()]
        return [row for row in rows if "event" in row and (not names or row["event"] in names)]

    def probes(self, router, verdicts):
        """Patch the inference probe to answer by producer URL; None is the standalone probe."""
        by_url = {url: iid for iid, url in self.urls.items()}

        async def probe(url, *, prefill_url=None, deadline_s=None):
            return verdicts.get(by_url.get(prefill_url), PASSED)

        return patch.object(router.engines, "probe_inference", side_effect=probe)

    async def stream_failure(self, router, iid, producer):
        """Fail one decode leg on `iid` after a transfer from `producer`, then settle probes."""
        router.verifier.leg_failed(iid, STREAM_FAILURE, prefill_iid=producer, decode_leg=True)
        await asyncio.gather(*router.verifier.tasks)

    def live(self, router, role=None):
        return sorted(i.iid for i in router.scheduler.live_instances(role))


class ProducerSelectionTests(HeldEngineTests):
    """The inference probe of a held engine avoids an unavailable recorded producer."""

    async def test_an_ejected_producer_gives_way_to_another_live_producer(self):
        router = self.router()
        router.scheduler.eject("e0", "connection")
        with self.probes(router, {}) as probe:
            await self.stream_failure(router, "e3", "e0")
        self.assertEqual(
            [call.kwargs["prefill_url"] for call in probe.call_args_list], [self.urls["e1"]]
        )
        self.assertIn("e3", self.live(router))
        self.assertNotIn("e3", router.scheduler.inference_suspects)

    async def test_a_held_producer_gives_way_to_another_live_producer(self):
        router = self.router()
        for until in (5.0, math.inf):
            with self.subTest(until=until):
                router.scheduler.quarantined.clear()
                self.assertTrue(router.scheduler.quarantine("e0", until))
                with self.probes(router, {}) as probe:
                    await self.stream_failure(router, "e3", "e0")
                self.assertEqual(probe.call_args.kwargs["prefill_url"], self.urls["e1"])
                self.assertIn("e3", self.live(router))

    async def test_a_down_producer_gives_way_to_the_next_producer(self):
        """A failed producer leg tests nothing on the held engine, so another producer runs."""
        router = self.router()
        with self.probes(router, {"e0": PRODUCER_DOWN}) as probe:
            await self.stream_failure(router, "e3", "e0")
        self.assertEqual(
            [call.kwargs["prefill_url"] for call in probe.call_args_list],
            [self.urls["e0"], self.urls["e1"]],
        )
        self.assertIn("e3", self.live(router))
        outcomes = [
            (row["outcome"], row["producer"], row["recorded_producer"])
            for row in self.events(router, "engine_probe")
        ]
        self.assertEqual(outcomes, [("producer_failed", "e0", "e0"), ("passed", "e1", "e0")])

    async def test_without_a_live_producer_the_probe_runs_standalone(self):
        router = self.router()
        with self.probes(router, {"e0": PRODUCER_DOWN, "e1": PRODUCER_DOWN}) as probe:
            await self.stream_failure(router, "e3", "e0")
        self.assertEqual(
            [call.kwargs["prefill_url"] for call in probe.call_args_list],
            [self.urls["e0"], self.urls["e1"], None],
        )
        self.assertIn("e3", self.live(router))

    async def test_an_unpinned_engine_of_the_other_role_can_produce_for_the_probe(self):
        router = self.router()
        router.scheduler.eject("e0", "connection")
        router.scheduler.eject("e1", "connection")
        with self.probes(router, {}) as probe:
            await self.stream_failure(router, "e4", "e0")
        # Like request placement, the probe takes a prefill leg on an unpinned engine.
        self.assertEqual(
            [call.kwargs["prefill_url"] for call in probe.call_args_list], [self.urls["e3"]]
        )

    async def test_with_every_producer_out_a_pinned_fleet_probes_standalone(self):
        router = self.router(engines=[replace(spec, pin=True) for spec in self.specs()])
        router.scheduler.eject("e0", "connection")
        router.scheduler.eject("e1", "connection")
        with self.probes(router, {}) as probe:
            await self.stream_failure(router, "e4", "e0")
        self.assertEqual([call.kwargs["prefill_url"] for call in probe.call_args_list], [None])
        self.assertIn("e4", self.live(router))

    async def test_a_control_pool_timeout_on_the_producer_leg_defers_without_fallback(self):
        router = self.router()

        with self.probes(router, {"e0": POOL_TIMEOUT}) as probe:
            await self.stream_failure(router, "e3", "e0")
        self.assertEqual(probe.await_count, 1)
        self.assertNotIn("e3", self.live(router))
        self.assertEqual(router.state()["holds"]["inference"][0]["recorded_producers"], ["e0"])

    async def test_a_failed_consumer_leg_through_a_substitute_ejects_the_held_engine(self):
        router = self.router()
        router.scheduler.eject("e0", "connection")
        failed = InferenceProbe(ProbeLeg(), ProbeLeg(failed="stream"))
        with self.probes(router, {"e1": failed}):
            await self.stream_failure(router, "e3", "e0")
        self.assertIn("e3", router.scheduler.ejected)
        ejected = self.events(router, "engine_ejected")[-1]
        self.assertEqual((ejected["iid"], ejected["cause"]), ("e3", "inference_probe"))

    async def test_producer_kill_returns_every_held_engine_within_readmit_every(self):
        """Engines held after a producer kill return through another live producer."""
        router = self.router(readmit_every=3)

        # The killed producer's transfers fail on both decode engines; their first
        # probes wait out the control pool, so both holds outlive them.
        with self.probes(router, {"e0": POOL_TIMEOUT}):
            await self.stream_failure(router, "e3", "e0")
            await self.stream_failure(router, "e4", "e0")
        self.assertEqual(
            sorted(row["iid"] for row in router.state()["holds"]["inference"]),
            ["e3", "e4"],
        )
        router.verifier.leg_failed("e0", httpx.ConnectError("refused"))
        self.assertIn("e0", router.scheduler.ejected)
        passes = 0
        with (
            self.probes(router, {"e0": PRODUCER_DOWN}),
            patch.object(router.engines, "healthy", new=AsyncMock(return_value=False)),
        ):
            while router.state()["holds"]["inference"] and passes <= router.cfg.readmit_every:
                self.now[0] += router.cfg.monitor_interval_s
                await monitor_once(router)
                await asyncio.gather(*router.verifier.tasks)
                passes += 1
        self.assertEqual(router.state()["holds"]["inference"], [])
        self.assertLessEqual(passes, router.cfg.readmit_every)
        self.assertTrue({"e3", "e4"} <= set(self.live(router, Role.DECODE)))
        self.assertNotIn("e0", self.live(router))


class HoldEventTests(HeldEngineTests):
    """Hold, ejection, probe and readmission transitions write events and counters."""

    async def test_timed_quarantine_starts_expires_and_counts(self):
        router = self.router()
        self.assertTrue(router.scheduler.quarantine("e0", 5.0))
        state = router.state()
        self.assertEqual(state["quarantined"], ["e0"])
        self.assertEqual(
            state["holds"],
            {"timed": [{"iid": "e0", "held_s": 0.0, "remaining_s": 5.0}], "inference": []},
        )
        self.now[0] += 6.0
        self.assertEqual(router.state()["holds"], {"timed": [], "inference": []})
        started, ended = self.events(router, "engine_hold_started", "engine_hold_ended")
        self.assertEqual(
            {k: started[k] for k in ("iid", "kind", "duration_s")},
            {"iid": "e0", "kind": "timed", "duration_s": 5.0},
        )
        self.assertEqual(
            {k: ended[k] for k in ("iid", "kind", "cause", "held_s")},
            {"iid": "e0", "kind": "timed", "cause": "expired", "held_s": 6.0},
        )
        breaker = router.state()["breaker"]
        self.assertEqual(breaker["hold_starts"], [{"iid": "e0", "kind": "timed", "count": 1}])
        self.assertEqual(
            breaker["hold_ends"],
            [{"iid": "e0", "kind": "timed", "cause": "expired", "count": 1}],
        )

    async def test_an_inference_hold_supersedes_a_timed_quarantine_until_verification(self):
        router = self.router(failure_quarantine_s=5.0)
        with self.probes(router, {}), patch.object(router.verifier, "start"):
            router.scheduler.quarantine("e3", 5.0)
            router.verifier.leg_failed("e3", STREAM_FAILURE, prefill_iid="e0", decode_leg=True)
        state = router.state()
        self.assertEqual(state["holds"]["timed"], [])
        self.assertEqual(
            state["holds"]["inference"],
            [{"iid": "e3", "held_s": 0.0, "recorded_producers": ["e0"]}],
        )
        self.assertEqual(state["quarantined"], ["e3"])
        self.now[0] += 60.0
        # An inference hold has no deadline.
        self.assertNotIn("e3", self.live(router))
        with self.probes(router, {}):
            await router.verifier.verify_inference("e3", self.urls["e3"])
        self.assertIn("e3", self.live(router))
        transitions = [
            (row["event"], row["kind"], row.get("cause"))
            for row in self.events(router, "engine_hold_started", "engine_hold_ended")
        ]
        self.assertEqual(
            transitions,
            [
                ("engine_hold_started", "timed", None),
                ("engine_hold_ended", "timed", "superseded"),
                ("engine_hold_started", "inference", None),
                ("engine_hold_ended", "inference", "verification"),
            ],
        )

    async def test_ejection_and_readmission_write_events_and_count_by_cause(self):
        router = self.router()
        router.scheduler.quarantine("e0", 5.0)
        router.verifier.leg_failed("e0", httpx.ConnectError("refused"))
        self.assertIn("e0", router.scheduler.ejected)
        self.now[0] += 1.0
        with patch.object(router.engines, "healthy", new=AsyncMock(return_value=True)):
            self.assertEqual(await readmit(router, 0), ["e0"])
        rows = [
            (row["event"], row.get("cause") or row.get("evidence"))
            for row in self.events(router)
            if row["event"].startswith("engine_")
        ]
        self.assertEqual(
            rows,
            [
                ("engine_hold_started", None),
                ("engine_ejected", "connection"),
                ("engine_hold_ended", "ejected"),
                ("engine_readmitted", "health"),
            ],
        )
        breaker = router.state()["breaker"]
        self.assertEqual(breaker["ejections"], [{"iid": "e0", "cause": "connection", "count": 1}])
        self.assertEqual(breaker["readmissions"], [{"iid": "e0", "evidence": "health", "count": 1}])
        router.scheduler.eject("e1", "liveness")
        router.scheduler.drain("e1")
        router.scheduler.finish_drain("e1")
        self.assertEqual(self.events(router, "engine_readmitted")[-1]["evidence"], "lifecycle")

    async def test_probe_outcomes_write_events_and_count_by_kind(self):
        router = self.router()
        with patch.object(
            router.engines, "healthy", new=AsyncMock(side_effect=[None, True, False])
        ):
            for _ in range(3):
                await router.verifier._verify_health("e3", self.urls["e3"])
        with patch.object(router.engines, "probe_inference", new=AsyncMock(return_value=None)):
            await router.verifier.verify_inference("e4", self.urls["e4"])
        probes = router.state()["breaker"]["probes"]
        self.assertEqual(
            [(row["iid"], row["kind"], row["outcome"], row["count"]) for row in probes],
            [
                ("e3", "verify_health", "failed", 1),
                ("e3", "verify_health", "inconclusive", 1),
                ("e3", "verify_health", "passed", 1),
                ("e4", "verify_inference", "unavailable", 1),
            ],
        )
        self.assertEqual(len(self.events(router, "engine_probe")), 4)
        self.assertEqual(self.events(router, "engine_ejected")[-1]["cause"], "health_probe")

    async def test_state_schema_and_metrics_carry_holds_and_counters(self):
        router = self.router()
        router.scheduler.quarantine("e0", 5.0)
        with patch.object(router.verifier, "start"):
            router.verifier.leg_failed("e3", STREAM_FAILURE, prefill_iid="e1", decode_leg=True)
        router.scheduler.eject("e4", "liveness")
        with self.probes(router, {}):
            await router.verifier.verify_inference("e3", self.urls["e3"])
        router.scheduler.quarantine("e3", 5.0)
        state = router.state()
        validated = StateOut.model_validate(state).model_dump(mode="json", by_alias=True)
        self.assertEqual(validated["holds"], state["holds"])
        self.assertEqual(validated["breaker"]["probes"], state["breaker"]["probes"])
        text = render(state, router.ttft, router.tpot)
        for line in (
            'narwhal_engine_held{iid="e0",kind="timed"} 1',
            'narwhal_engine_held{iid="e3",kind="timed"} 1',
            'narwhal_engine_ejections_total{iid="e4",cause="liveness"} 1',
            'narwhal_engine_hold_starts_total{iid="e3",kind="inference"} 1',
            'narwhal_engine_hold_ends_total{iid="e3",kind="inference",cause="verification"} 1',
            'narwhal_engine_probes_total{iid="e3",kind="verify_inference",outcome="passed"} 1',
            'narwhal_engine_quarantined{iid="e3"} 1',
        ):
            self.assertIn(line, text)
        self.assertIn("# TYPE narwhal_engine_readmissions_total counter", text)


class FloorRestorationTests(HeldEngineTests):
    """Decode and prefill floors recover after ejections and holds."""

    async def test_decode_floor_restores_after_an_inference_hold_and_an_ejection(self):
        for cause in ("hold", "ejection"):
            with self.subTest(cause=cause):
                router = self.router(min_decode=2)
                if cause == "hold":
                    # The first probe waits out the control pool and keeps the hold.
                    with self.probes(router, {"e0": POOL_TIMEOUT}):
                        await self.stream_failure(router, "e3", "e0")
                else:
                    router.scheduler.eject("e3", "liveness")
                self.assertEqual(router.scheduler.decode_live(), 1)
                with patch.object(router.engines, "healthy", new=AsyncMock(return_value=False)):
                    await monitor_once(router)
                self.assertEqual(router.scheduler.decode_live(), 2)
                restored = self.events(router, "decode_floor_restored")
                self.assertEqual(len(restored), 1)
                self.assertEqual(restored[0]["live_decode"], 2)
                decode_floor = router.state()["decode_floor"]
                self.assertEqual(decode_floor["restoration_moves"], 1)
                self.assertFalse(decode_floor["below_floor"])
                # The held or ejected engine returns on passing evidence.
                with (
                    self.probes(router, {}),
                    patch.object(router.engines, "healthy", new=AsyncMock(return_value=True)),
                ):
                    self.now[0] += router.cfg.monitor_interval_s * router.cfg.readmit_every
                    await monitor_once(router)
                    await asyncio.gather(*router.verifier.tasks)
                self.assertIn("e3", self.live(router, Role.DECODE))
                self.assertEqual(router.scheduler.decode_live(), 3)

    async def test_prefill_floor_recovers_after_a_timed_hold_and_an_ejection(self):
        for cause in ("hold", "ejection"):
            with self.subTest(cause=cause):
                router = self.router(min_prefill=2)
                if cause == "hold":
                    self.assertTrue(router.scheduler.quarantine("e0", 5.0))
                else:
                    router.scheduler.eject("e0", "connection")
                self.assertTrue(router.state()["below_floor"]["active"])
                below = self.events(router, "below_floor")
                self.assertEqual(len(below), 1)
                self.assertEqual(below[0]["live_prefill"], 1)
                self.assertEqual(below[0]["quarantined"], ["e0"] if cause == "hold" else [])
                self.now[0] += 6.0
                with patch.object(router.engines, "healthy", new=AsyncMock(return_value=True)):
                    await readmit(router, 0)
                    await monitor_once(router)
                self.assertIn("e0", self.live(router))
                self.assertFalse(router.state()["below_floor"]["active"])
                recovered = self.events(router, "below_floor_recovered")
                self.assertEqual(len(recovered), 1)
                self.assertGreaterEqual(recovered[0]["live_prefill"], 2)
                self.assertGreaterEqual(recovered[0]["duration_s"], 6.0)

    async def test_prefill_floor_holds_while_a_held_decode_engine_verifies_elsewhere(self):
        """A held decode engine verified through a live producer leaves the prefill floor met."""
        router = self.router(min_prefill=2)
        with self.probes(router, {"e0": PRODUCER_DOWN}):
            await self.stream_failure(router, "e3", "e0")
        self.assertFalse(self.events(router, "below_floor"))
        self.assertIn("e3", self.live(router, Role.DECODE))
        self.assertEqual(self.live(router, Role.PREFILL), ["e0", "e1"])


class DroppedConnectionTests(HeldEngineTests):
    """A connection that drops after it was established is timeout evidence."""

    def test_a_dropped_connection_runs_a_health_probe_under_a_timed_hold(self):
        router = self.router(failure_quarantine_s=5.0)
        dropped = (
            httpx.ReadError("engine connection lost: reset"),
            httpx.RemoteProtocolError(
                "peer closed connection without sending complete message body"
            ),
        )
        for exc in dropped:
            for decode in (False, True):
                with self.subTest(exc=type(exc).__name__, decode=decode):
                    iid = "e3" if decode else "e0"
                    state = SimpleNamespace(
                        router=router,
                        prefill_iid="e0" if decode else None,
                        output_started=True,
                        failed_engines={"prefill": set(), "decode": set()},
                    )
                    with patch.object(router.verifier, "start") as start:
                        _failed_leg(
                            state, router.monitor.instances[iid], exc, decode=decode, started=0.0
                        )
                    start.assert_called_once_with(iid, "verify_health")
                    self.assertEqual(router.state()["breaker"]["failures"][iid]["timeout"], 1)
                    self.assertNotIn(iid, router.scheduler.inference_suspects)
                    self.assertEqual(
                        [row["iid"] for row in router.state()["holds"]["timed"]], [iid]
                    )
                    self.assertEqual(
                        failure_reason(exc, deadline_passed=False), "engine_connection"
                    )
                    router.scheduler.verifying.clear()
                    router.scheduler.record_answer(iid, "health")
        # A refused connection ejects at once instead.
        router.verifier.leg_failed("e0", httpx.ConnectError("refused"))
        self.assertIn("e0", router.scheduler.ejected)


if __name__ == "__main__":
    unittest.main()
