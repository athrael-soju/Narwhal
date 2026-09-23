"""Check monitoring failure isolation, admission fencing and health recovery."""

import asyncio
import json
import tempfile
import unittest
from contextlib import ExitStack, suppress
from pathlib import Path
from unittest.mock import AsyncMock, patch

from narwhal.runtime import monitoring
from narwhal.runtime.lifecycle import ValidationOutcome
from narwhal.runtime.monitoring import MonitoringLedger, monitor_once, readmit, sweep_liveness
from narwhal.serving.app import create_app
from tests.fixtures import fleet


class MonitoringLedgerTests(unittest.TestCase):
    """Track failed monitoring passes and failures in each stage."""

    def test_multiple_failed_stages_count_one_pass_and_clean_pass_recovers(self):
        """At the failure limit, the degraded status reports the first failed stage."""
        events = []
        ledger = MonitoringLedger(clock=lambda: 10, on_event=events.append)
        with self.assertLogs("narwhal.monitoring_loop", level="ERROR"):
            ledger.fail("controller", ValueError("sensitive-detail"))
            ledger.fail("health", OSError("storage"))
        ledger.finish_pass(2)
        self.assertEqual(ledger.core_failures, 1)
        self.assertEqual(ledger.degraded, "")
        with self.assertLogs("narwhal.monitoring_loop", level="ERROR"):
            ledger.fail("handoff", OSError("storage"))
        ledger.finish_pass(2)
        self.assertEqual(ledger.degraded, "ValueError:controller")
        self.assertNotIn("sensitive-detail", json.dumps(events))
        self.assertNotIn("sensitive-detail", json.dumps(ledger.snapshot()))
        ledger.ok("controller")
        ledger.finish_pass(2)
        self.assertEqual(ledger.degraded, "")
        self.assertEqual(ledger.core_failures, 2)
        self.assertEqual(events[-1]["event"], "monitoring_recovered")

    def test_event_writer_failure_leaves_failure_accounting_available(self):
        """A journal write failure leaves admission blocked by the monitoring error."""

        def broken(row):
            raise OSError("journal unavailable")

        ledger = MonitoringLedger(on_event=broken)
        with self.assertLogs("narwhal.monitoring_loop", level="ERROR"):
            ledger.fail("handoff", OSError("disk"))
            ledger.finish_pass(1)
        self.assertEqual(ledger.degraded, "OSError:handoff")

    def test_event_loop_lag_tracks_current_and_process_high_water(self):
        ledger = MonitoringLedger()
        ledger.observe_event_loop_lag(0.03)
        ledger.observe_event_loop_lag(0.01)
        snapshot = ledger.snapshot()
        self.assertEqual(snapshot["event_loop_lag_s"], 0.01)
        self.assertEqual(snapshot["event_loop_lag_high_water_s"], 0.03)
        ledger.observe_event_loop_lag(-1.0)
        self.assertEqual(ledger.snapshot()["event_loop_lag_s"], 0.0)


class MonitoringPassTests(unittest.IsolatedAsyncioTestCase):
    """Real controller passes use mocked health probes and local handoff storage."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        cfg = fleet(root)
        cfg.engine_contract = None
        cfg.state_path = root / "handoff.json"
        cfg.monitor_failure_limit = 1
        cfg.liveness_every = 0
        self.router = create_app(cfg).state.router
        self.addAsyncCleanup(self.router.engines.aclose)

    async def test_controller_failure_preserves_cleanup_and_handoff_stages(self):
        """Later monitoring stages still execute after controller sampling fails."""
        with (
            patch.object(self.router.controller, "sample", side_effect=ValueError("sample")),
            patch.object(
                self.router.scheduler, "settle_drains", wraps=self.router.scheduler.settle_drains
            ) as drains,
            self.assertLogs("narwhal.monitoring_loop", level="ERROR"),
        ):
            await monitor_once(self.router)
        self.assertEqual(drains.call_count, 1)
        self.assertTrue(self.router.cfg.state_path.is_file())
        self.assertTrue(self.router.monitoring_degraded)
        await monitor_once(self.router)
        self.assertFalse(self.router.monitoring_degraded)

    async def test_prefill_risk_wakes_control_before_the_periodic_pass(self):
        """One event runs urgent control between full monitoring passes."""
        self.router.cfg.monitor_interval_s = 10.0
        called = asyncio.Event()

        async def urgent(_router):
            called.set()

        with (
            patch.object(monitoring, "urgent_control_once", side_effect=urgent) as urgent_once,
            patch.object(monitoring, "monitor_once", new=AsyncMock()) as periodic,
        ):
            task = asyncio.create_task(monitoring.monitor_loop(self.router))
            await asyncio.sleep(0)
            self.router.control_wakeup.set()
            await asyncio.wait_for(called.wait(), timeout=1.0)
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        urgent_once.assert_awaited_once_with(self.router)
        periodic.assert_not_awaited()

    async def test_periodic_monitor_records_event_loop_deadline_lag(self):
        self.router.cfg.monitor_interval_s = 0.01
        called = asyncio.Event()

        async def periodic(_router, *, urgent=False):
            called.set()

        with (
            patch.object(monitoring, "monitor_once", side_effect=periodic),
            patch.object(
                self.router.monitoring,
                "observe_event_loop_lag",
                wraps=self.router.monitoring.observe_event_loop_lag,
            ) as observe,
        ):
            task = asyncio.create_task(monitoring.monitor_loop(self.router))
            await asyncio.wait_for(called.wait(), timeout=1.0)
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        observe.assert_called()
        self.assertGreaterEqual(observe.call_args.args[0], 0.0)

    async def test_urgent_control_preserves_demand_sampling_cadence(self):
        """Scheduled monitoring passes retain ownership of demand sampling."""
        with (
            patch.object(self.router.controller, "sample") as sample,
            patch.object(self.router.controller, "step", return_value=None) as step,
        ):
            self.assertIsNone(monitoring.run_controller(self.router, urgent=True, scheduled=False))
        sample.assert_not_called()
        step.assert_called_once_with(urgent=True, scheduled=False)

    async def test_control_pool_exhaustion_preserves_liveness_miss_count(self):
        """Pool exhaustion leaves the engine's liveness miss count unchanged."""
        self.router.scheduler.liveness_misses["e0"] = 1
        with patch.object(self.router.engines, "healthy", AsyncMock(return_value=None)):
            self.assertEqual(await sweep_liveness(self.router), [])
        self.assertEqual(self.router.scheduler.liveness_misses["e0"], 1)
        with patch.object(self.router.engines, "healthy", AsyncMock(return_value=True)):
            self.assertEqual(await sweep_liveness(self.router), [])
        self.assertEqual(self.router.scheduler.liveness_misses.get("e0", 0), 0)

    async def test_health_readmission_requires_positive_evidence(self):
        """Only an explicit healthy answer releases an ejected engine."""
        self.router.scheduler.ejected["e0"] = self.router._clock() - 100
        with patch.object(self.router.engines, "healthy", AsyncMock(return_value=None)):
            self.assertEqual(await readmit(self.router, 1), [])
        self.assertIn("e0", self.router.scheduler.ejected)
        with patch.object(self.router.engines, "healthy", AsyncMock(return_value=True)) as healthy:
            self.assertEqual(await readmit(self.router, 1), [])
            healthy.assert_not_awaited()
            self.assertEqual(await readmit(self.router, 0), ["e0"])
        self.assertNotIn("e0", self.router.scheduler.ejected)

    async def test_each_guarded_stage_records_failure_and_allows_the_pass_to_finish(self):
        """Monitoring recovers on the first clean pass after a stage failure."""
        for stage, owner, name in (
            ("health", self.router.scheduler, "health_pass"),
            ("drains", self.router.scheduler, "settle_drains"),
            ("rollover", self.router.monitor, "roll_interval"),
            ("readmission", monitoring, "readmit"),
            ("liveness", monitoring, "sweep_liveness"),
            ("telemetry", self.router.scheduler, "pool_load"),
            ("handoff", monitoring.handoff_state, "write"),
        ):
            self.router.cfg.liveness_every = 1
            with (
                self.subTest(stage=stage),
                patch.object(owner, name, side_effect=RuntimeError("injected failure")),
                patch.object(self.router.scheduler, "sweep_due", return_value=True),
                patch.object(self.router.engines, "healthy", new=AsyncMock(return_value=True)),
                patch.object(self.router.dispatcher, "notify") as notify,
                self.assertLogs("narwhal.monitoring_loop", level="ERROR"),
            ):
                await monitor_once(self.router)
            self.assertGreaterEqual(self.router.monitoring.stages[stage].failures, 1)
            self.assertTrue(self.router.monitoring.degraded)
            notify.assert_called_once()
            self.router.cfg.liveness_every = 0
            await monitor_once(self.router)
            self.assertEqual(self.router.monitoring.degraded, "")

    async def test_liveness_ejects_after_consecutive_failures_and_skips_held_engines(self):
        """A health miss reaches ejection at the configured count and clears that counter."""
        self.router.cfg.liveness_misses = 2
        self.router.scheduler.drain("e3")
        with patch.object(
            self.router.engines, "healthy", new=AsyncMock(return_value=False)
        ) as healthy:
            self.assertEqual(await sweep_liveness(self.router), [])
            self.assertEqual(await sweep_liveness(self.router), ["e0"])
            self.assertEqual(await sweep_liveness(self.router), [])
        self.assertEqual(healthy.await_count, 2)
        self.assertNotIn("e0", self.router.scheduler.liveness_misses)
        self.assertIn("e0", self.router.scheduler.ejected)

    async def test_automatic_contract_recovery_requires_passing_complete_evidence(self):
        """The engine stays blocked if validation fails, raises or loses fleet control."""
        self.router.cfg.engine_contract = fleet(
            self.router.cfg.profiles_path.parent
        ).engine_contract
        for outcome, fenced in (
            (ValidationOutcome(failures={"e0": ["failed"]}), False),
            (ValueError("validator failed"), False),
            (ValidationOutcome(starts={"e0": 101}), True),
        ):
            self.router.lifecycle.records.clear()
            self.router.scheduler.draining.clear()
            self.router.scheduler.ejected["e0"] = self.router._clock() - 100
            with self.subTest(outcome=outcome, fenced=fenced), ExitStack() as stack:
                stack.enter_context(
                    patch.object(self.router.engines, "healthy", new=AsyncMock(return_value=True))
                )
                stack.enter_context(
                    patch.object(monitoring, "controls_fleet", return_value=not fenced)
                )
                stack.enter_context(
                    patch.object(
                        monitoring,
                        "validate_readmission",
                        new=AsyncMock(
                            side_effect=outcome if isinstance(outcome, Exception) else None,
                            return_value=outcome,
                        ),
                    )
                )
                stack.enter_context(self.assertLogs("narwhal.monitoring_loop", level="WARNING"))
                self.assertEqual(await readmit(self.router, 0), [])
            self.assertEqual(self.router.lifecycle.records["e0"].state, "blocked")
            self.assertIn("e0", self.router.scheduler.ejected)

    async def test_full_outage_recovery_waits_for_every_peer_and_readmits_atomically(self):
        """Recovery from a total outage waits for every engine to pass health and wave checks."""
        self.router.cfg.engine_contract = fleet(
            self.router.cfg.profiles_path.parent
        ).engine_contract
        for iid in ("e0", "e3"):
            self.router.scheduler.eject(iid)
        with patch.object(self.router.engines, "healthy", new=AsyncMock(side_effect=[True, False])):
            self.assertEqual(await readmit(self.router, 0), [])
        self.assertFalse(self.router.lifecycle.records)
        with (
            patch.object(self.router.engines, "healthy", new=AsyncMock(return_value=True)),
            patch.object(
                monitoring,
                "validate_readmission",
                new=AsyncMock(return_value=ValidationOutcome(starts={"e0": 101, "e3": 102})),
            ) as validate,
        ):
            self.assertEqual(await readmit(self.router, 0), ["e0", "e3"])
        self.assertTrue(validate.call_args.kwargs["wave"])
        self.assertEqual(self.router.lifecycle.wave_id, "")
        self.assertEqual(self.router.scheduler.draining, set())

    async def test_whole_wave_policy_converts_automatic_recovery_to_operator_hold(self):
        """Whole-wave policy holds every engine before any automatic health readmission."""
        self.router.cfg.engine_restart_policy = "whole_wave"
        self.router.scheduler.eject("e0")
        with patch.object(self.router.engines, "healthy", new=AsyncMock()) as healthy:
            self.assertEqual(await readmit(self.router, 0), [])
        healthy.assert_not_awaited()
        self.assertEqual(self.router.scheduler.draining, {"e0", "e3"})
        self.assertTrue(self.router.lifecycle_blocked)
