"""Deterministic CPU regressions for adjacent reactive role changes."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx

from narwhal.config import EngineSpec, FleetConfig
from narwhal.observability.journal import RunJournal
from narwhal.profiling.model import Profile
from narwhal.profiling.store import ProfileStore
from narwhal.scheduling.control import SLO, Thresholds
from narwhal.scheduling.controller import ReactiveController
from narwhal.scheduling.demand import Demand
from narwhal.scheduling.monitor import InstanceMonitor
from narwhal.scheduling.scheduler import GlobalScheduler
from narwhal.serving.policy import ServingPolicy
from narwhal.serving.response import RequestStreamResponse
from narwhal.serving.router import NarwhalRouter
from narwhal.types import Instance, Phase, Request, Role


class Fleet:
    """Real policy, demand, scoring and scheduler with controlled telemetry and time."""

    def __init__(self, directory: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.now = 0.0
        self.profiles = ProfileStore(Path(directory) / "profiles.json")
        self.monitor = InstanceMonitor(clock=lambda: self.now, profiles=self.profiles)
        for index in range(6):
            iid = f"e{index}"
            self.profiles.put(
                Profile(
                    iid,
                    0.0,
                    0.001,
                    0.0,
                    0.000001,
                    0.001,
                    kv_capacity_tokens=100_000,
                    decode_min_requests=1,
                    decode_max_requests=100,
                    decode_min_kv_tokens=1,
                    decode_max_kv_tokens=100_000,
                    decode_fit_mape=0.0,
                    decode_cv_mape=0.0,
                )
            )
            self.monitor.add(
                Instance(iid, f"http://{iid}", Role.PREFILL if index == 0 else Role.DECODE)
            )
        self.scheduler = GlobalScheduler(
            self.monitor,
            self.profiles,
            SLO(1.0, 0.01),
            Thresholds(cooldown_s=10.0, dwell_s=20.0),
            clock=lambda: self.now,
        )
        self.controller = ReactiveController(
            self.monitor,
            self.scheduler,
            clock=lambda: self.now,
            window_s=120.0,
            confirmations=2,
            utilization=0.8,
            min_arrivals=10,
            demand_floor=0.1,
            step_s=5.0,
            evidence_span_s=60.0,
            evidence_max_span_s=120.0,
            evidence_min_arrivals=10,
        )
        if transport is not None:
            cfg = FleetConfig(
                model="stub",
                engines=[EngineSpec(i.iid, i.url, i.role) for i in self.monitor.instances.values()],
                slo=self.scheduler.slo,
                thresholds=self.scheduler.th,
                profiles_path=self.profiles.path,
                reactive_demand_floor=0.1,
                admission="off",
                tokenize=False,
                serving=ServingPolicy(
                    queue_capacity=8,
                    queue_timeout_s=10.0,
                    prefill_concurrency=1,
                    decode_concurrency=100,
                    handoff_timeout_s=60.0,
                ),
            )
            self.journal = RunJournal(Path(directory) / "journal.jsonl")
            self.journal.open()
            self.router = NarwhalRouter(cfg, self.journal, transport, clock=lambda: self.now)
            self.profiles = self.router.profiles
            self.monitor = self.router.monitor
            self.scheduler = self.router.scheduler
            self.controller = self.router.controller
        self.pressure = {Role.PREFILL: 6.0, Role.DECODE: 6.0}
        self.telemetry = patch.object(
            self.scheduler,
            "pool_load",
            side_effect=lambda role: self.pressure[role] / len(self.monitor.pool(role)),
        )
        self.telemetry.start()
        self.advance(60)

    def advance(self, seconds: int = 5) -> None:
        for _ in range(seconds):
            self.controller.saw_arrival(100, wanted_len=10, at=self.now)
            self.now += 1.0

    def step(self):
        self.advance()
        return self.controller.step()

    def confirm(self):
        result = None
        for _ in range(
            max(self.controller.confirmations_needed, self.scheduler.th.sustained_intervals)
        ):
            result = self.step()
        return result


class MixedPressureTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.fleet = Fleet(directory.name)
        self.addCleanup(self.fleet.telemetry.stop)

    def test_prefill_recovers_above_decode_shrink_after_confirmation(self) -> None:
        fleet = self.fleet
        self.assertIsNone(fleet.step())
        decision = fleet.scheduler._last_decision
        self.assertEqual((decision["prefill"], decision["decode"]), (2, 4))
        self.assertGreater(decision["projected_tpot_ratio"], fleet.scheduler.th.shrink)
        self.assertGreater(decision["objective_delta"], fleet.controller.movement_margin)
        self.assertTrue(decision["decode_profile_covered"])
        self.assertTrue(decision["evidence_closed"])
        self.assertEqual(decision["reason"], "waiting for sustained demand")
        self.assertIsNone(fleet.step())
        self.assertIsNotNone(fleet.step())
        self.assertEqual(len(fleet.monitor.pool(Role.PREFILL)), 2)
        self.assertEqual(len(fleet.scheduler.flips), 1)

    def test_configured_confirmation_count_and_transient_reset(self) -> None:
        fleet = self.fleet
        fleet.controller.confirmations_needed = 4
        self.assertIsNone(fleet.step())
        fleet.pressure[Role.PREFILL] = 0.9
        self.assertIsNone(fleet.step())
        self.assertEqual(fleet.controller.reactive.confirmations, 0)
        fleet.pressure[Role.PREFILL] = 6.0
        for _ in range(3):
            self.assertIsNone(fleet.step())
        self.assertIsNotNone(fleet.step())

    def test_sustained_mixed_pressure_converges_without_oscillation(self) -> None:
        fleet = self.fleet
        for index in range(60):
            noise = 0.05 if index % 2 else -0.05
            fleet.pressure = {Role.PREFILL: 6.0 + noise, Role.DECODE: 6.0 - noise}
            fleet.step()
        self.assertEqual(len(fleet.monitor.pool(Role.PREFILL)), 3)
        self.assertEqual([flip.to for flip in fleet.scheduler.flips], [Role.PREFILL] * 2)
        self.assertGreater(fleet.scheduler.pool_load(Role.PREFILL), 1.0)
        self.assertGreater(fleet.scheduler.pool_load(Role.DECODE), 1.0)

    def test_candidate_uses_its_colocated_prefill_cost(self) -> None:
        """A 2P/4D proposal is priced from its own measured mix rows."""
        fleet = self.fleet
        for iid in fleet.monitor.instances:
            base = fleet.profiles.get(iid)
            for prefill, decode, multiplier in ((1, 5, 1.0), (2, 4, 10.0)):
                fleet.profiles.put(
                    replace(
                        base,
                        ttft_b=base.ttft_b * multiplier,
                        colocated_group="gpu-0",
                        colocated_target_role=(
                            Role.PREFILL.value
                            if iid == "e0" or (prefill == 2 and iid == "e1")
                            else Role.DECODE.value
                        ),
                        colocated_prefill_engines=prefill,
                        colocated_decode_engines=decode,
                        colocated_prefill_rps=1.0,
                        colocated_decode_rps=1.0,
                    )
                )
        fleet.profiles.bind_role_mix(
            dict.fromkeys(fleet.monitor.instances, "gpu-0"),
            lambda group: (1, 5),
            lambda iid: fleet.monitor.instances[iid].role,
        )
        fleet.controller._demand(fleet.now)
        snapshot = fleet.controller.scorer.capture(
            fleet.now,
            fleet.controller.last_demand,
            utilization=fleet.controller.utilization,
            observed_load=(0.0, 0.0),
            window_s=fleet.controller.window_s,
            step_s=fleet.controller.step_s,
        )
        self.assertEqual(len(snapshot.profile_options[1][1]), 6)
        self.assertGreater(
            snapshot.score(2).ttft_ratio,
            snapshot.score(1).ttft_ratio,
        )
        too_narrow = replace(
            snapshot,
            profile_options=(
                (
                    2,
                    tuple(
                        replace(p, prefill_max_tokens=50) for p in snapshot.profile_options[1][1]
                    ),
                ),
            ),
            offered_inputs=(100,),
        )
        self.assertFalse(too_narrow.score(2).decode_profile_covered)

    def test_prefill_below_expand_preserves_source_shrink_gate(self) -> None:
        fleet = self.fleet
        fleet.pressure[Role.PREFILL] = 0.9
        self.assertIsNone(fleet.confirm())
        self.assertEqual(fleet.scheduler._last_decision["eligibility_rule"], "source_shrink")
        self.assertEqual(fleet.scheduler.flips, [])

    def test_ordinary_urgent_consolidation_keeps_single_confirmation(self) -> None:
        fleet = self.fleet
        fleet.pressure[Role.DECODE] = 0.4
        self.assertIsNotNone(fleet.step())
        self.assertEqual(fleet.scheduler._last_decision["eligibility_rule"], "source_shrink")
        self.assertEqual(fleet.scheduler._last_decision["required_confirmations"], 1)

    def test_zero_margin_still_requires_strict_mixed_pressure_improvement(self) -> None:
        fleet = self.fleet
        fleet.controller.movement_margin = 0.0
        fleet.pressure = {Role.PREFILL: 1.5, Role.DECODE: 6.0}
        self.assertIsNone(fleet.confirm())
        self.assertEqual(fleet.scheduler._last_decision["objective_delta"], 0.0)
        self.assertEqual(fleet.scheduler.flips, [])

    def test_incomplete_demand_requires_observed_pressure(self) -> None:
        fleet = self.fleet
        fleet.controller.demand.unsized_pending = 1
        fleet.pressure = {Role.PREFILL: 0.9, Role.DECODE: 0.9}
        self.assertIsNone(fleet.confirm())
        self.assertIn("incomplete demand requires", fleet.scheduler._last_decision["reason"])
        fleet.pressure = {Role.PREFILL: 6.0, Role.DECODE: 6.0}
        self.assertIsNotNone(fleet.confirm())
        self.assertFalse(fleet.scheduler._last_decision["demand_complete"])
        self.assertEqual(
            fleet.scheduler._last_decision["decision_basis"], "prefill_pressure_recovery"
        )

    def test_missing_fleet_profile_blocks_mixed_pressure(self) -> None:
        fleet = self.fleet
        fleet.profiles._by_id.pop("e5")
        self.assertIsNone(fleet.confirm())
        self.assertEqual(
            fleet.scheduler._last_decision["reason"], "mixed pressure requires fleet profiles"
        )

    def queued_prefill(self) -> None:
        fleet = self.fleet
        fleet.controller.demand.unsized_pending = 1
        for index in range(4):
            fleet.monitor.dispatched("e0", Request(f"active{index}", 100, wanted_len=10))
        for index in range(20):
            request = Request(f"queued{index}", 100, wanted_len=10)
            fleet.monitor.waiting[request.rid] = request

    def test_backlog_recovers_across_active_pressure_dips(self) -> None:
        fleet = self.fleet
        self.queued_prefill()
        for index, pressure in enumerate((1.2, 0.8, 0.7)):
            fleet.pressure[Role.PREFILL] = pressure
            moved = fleet.step()
            self.assertEqual(moved is not None, index == 2)
            decision = fleet.scheduler._last_decision
            if index < 2:
                self.assertEqual(fleet.controller.reactive.confirmations, index + 1)
            self.assertFalse(decision["demand_complete"])
        self.assertGreater(decision["recovery_prefill_ratio"], 1)
        self.assertEqual(len(fleet.monitor.pool(Role.PREFILL)), 2)

    def test_clearing_backlog_resets_recovery_confirmation(self) -> None:
        fleet = self.fleet
        self.queued_prefill()
        fleet.pressure = {Role.PREFILL: 0.8, Role.DECODE: 0.4}
        self.assertIsNone(fleet.step())
        waiting = dict(fleet.monitor.waiting)
        fleet.monitor.waiting.clear()
        self.assertIsNone(fleet.step())
        self.assertEqual(fleet.controller.reactive.confirmations, 0)
        fleet.monitor.waiting.update(waiting)
        self.assertIsNone(fleet.step())
        self.assertIsNone(fleet.step())
        self.assertIsNotNone(fleet.step())

    def test_backlog_does_not_bypass_decode_capacity(self) -> None:
        fleet = self.fleet
        self.queued_prefill()
        fleet.monitor.waiting["large"] = Request("large", 100, wanted_len=1_000_000)
        fleet.pressure = {Role.PREFILL: 0.8, Role.DECODE: 0.4}
        self.assertIsNone(fleet.confirm())
        self.assertEqual(fleet.scheduler.flips, [])
        self.assertGreater(
            fleet.scheduler._last_decision["pending_decode_tokens"],
            fleet.scheduler._last_decision["decode_kv_capacity_tokens"],
        )

    def test_decode_waiters_do_not_supply_prefill_recovery_pressure(self) -> None:
        fleet = self.fleet
        self.queued_prefill()
        for request in fleet.monitor.waiting.values():
            request.phase = Phase.DECODE
        fleet.pressure = {Role.PREFILL: 0.8, Role.DECODE: 0.4}
        self.assertIsNone(fleet.confirm())
        self.assertEqual(fleet.scheduler._last_decision["queued_prefill_s"], 0)
        self.assertEqual(fleet.scheduler.flips, [])

    def test_resident_batch_outside_profile_blocks_move(self) -> None:
        fleet = self.fleet
        profile = fleet.profiles.get("e1")
        fleet.profiles.put(replace(profile, decode_max_requests=1))
        for index in range(2):
            fleet.monitor.dispatched("e1", Request(f"resident{index}", 100, phase=Phase.DECODE))
        self.assertIsNone(fleet.confirm())
        self.assertEqual(
            fleet.scheduler._last_decision["reason"], "decode profile does not cover proposed work"
        )
        self.assertEqual(len(fleet.monitor.instances["e1"].decode), 2)

    def test_sticky_decode_residents_keep_their_measured_batch_after_role_change(self) -> None:
        fleet = self.fleet
        for index in range(1, 6):
            iid = f"e{index}"
            profile = fleet.profiles.get(iid)
            fleet.profiles.put(replace(profile, decode_max_kv_tokens=1_200))
            fleet.monitor.dispatched(iid, Request(f"resident{index}", 1_000, phase=Phase.DECODE))
        snapshot = fleet.controller.scorer.capture(
            fleet.now,
            Demand(1.0, 0.1, 10, 10),
            utilization=0.8,
            observed_load=(0.0, 0.0),
        )
        candidate = snapshot.score(2)
        self.assertEqual(candidate.decode_tokens_per_engine, 1_000)
        self.assertTrue(candidate.decode_profile_covered)

    def test_pending_output_exceeding_kv_blocks_move(self) -> None:
        fleet = self.fleet
        # Pending output is priced before decode dispatch, even when all GPUs
        # would currently appear idle. Physical capacity also bounds the domain.
        fleet.monitor.waiting["pending"] = Request("pending", 1, wanted_len=1_000_000)
        self.assertIsNone(fleet.confirm())
        decision = fleet.scheduler._last_decision
        self.assertGreater(decision["pending_decode_tokens"], decision["decode_kv_capacity_tokens"])
        self.assertEqual(decision["pending_decode_requests"], 1)
        self.assertEqual(fleet.scheduler.flips, [])

    def test_waiting_decode_work_does_not_expand_the_active_batch(self) -> None:
        fleet = self.fleet
        for iid in fleet.monitor.instances:
            fleet.profiles.put(replace(fleet.profiles.get(iid), decode_max_requests=2))
        fleet.monitor.instances["e1"].role = Role.PREFILL
        fleet.monitor.instances["e2"].role = Role.PREFILL
        for iid, count in (("e3", 2), ("e4", 1), ("e5", 1)):
            for index in range(count):
                fleet.monitor.dispatched(
                    iid, Request(f"{iid}-active-{index}", 100, phase=Phase.DECODE)
                )
        for index in range(100):
            request = Request(f"queued-{index}", 100, wanted_len=10, phase=Phase.DECODE)
            fleet.monitor.waiting[request.rid] = request
        snapshot = fleet.controller.scorer.capture(
            fleet.now,
            Demand(0.1, 3.0, 30, 0),
            utilization=0.8,
            observed_load=(0.1, 1.0),
        )
        expand_decode = snapshot.score(2)
        shrink_decode = snapshot.score(4)
        self.assertTrue(expand_decode.decode_profile_covered)
        self.assertLessEqual(expand_decode.decode_requests_per_engine, 2)
        self.assertTrue(shrink_decode.decode_profile_covered)
        self.assertGreater(shrink_decode.decode_queue_ratio, expand_decode.decode_queue_ratio)
        self.assertEqual(shrink_decode.pending_decode_requests, 100)

    def test_idle_candidate_decoder_does_not_create_fractional_batch(self) -> None:
        fleet = self.fleet
        fleet.monitor.instances["e1"].role = Role.PREFILL
        fleet.monitor.instances["e2"].role = Role.PREFILL
        for iid in ("e3", "e4", "e5"):
            fleet.monitor.dispatched(iid, Request(f"{iid}-active", 100, phase=Phase.DECODE))
        snapshot = fleet.controller.scorer.capture(
            fleet.now,
            Demand(0.1, 2.0, 30, 0),
            utilization=0.8,
            observed_load=(0.1, 1.0),
        )
        expanded = snapshot.score(2)
        self.assertEqual(expanded.decode_requests_per_engine, 0.75)
        self.assertTrue(expanded.decode_profile_covered)

    def test_flip_resident_guard_blocks_a_heavy_donor(self) -> None:
        fleet = self.fleet
        fleet.scheduler.th.flip_resident_guard = 2
        for iid in ("e1", "e2", "e3", "e4", "e5"):
            for index in range(3):
                fleet.monitor.dispatched(iid, Request(f"{iid}-r{index}", 100, phase=Phase.DECODE))
        self.assertIsNone(fleet.confirm())
        self.assertIn("flip_resident_guard", fleet.scheduler._last_decision["reason"])
        self.assertEqual(fleet.scheduler.flips, [])
        fleet.scheduler.th.flip_resident_guard = 3
        self.assertIsNotNone(fleet.confirm())

    def test_evidence_risk_window_is_not_bypassed(self) -> None:
        fleet = self.fleet
        fleet.controller.safety.note_risk_event("first_token_timeout")
        self.assertIsNone(fleet.confirm())
        self.assertEqual(fleet.scheduler._last_decision["evidence_blocked_gate"], "risk")
        fleet.advance(60)
        self.assertIsNotNone(fleet.confirm())

    def test_rising_decode_demand_blocks_move(self) -> None:
        fleet = self.fleet
        # Fill the long window before the burst so its denominator differs
        # from the short horizon by enough to exercise the trend threshold.
        fleet.advance(60)
        for index in range(200):
            fleet.controller.saw_arrival(100, wanted_len=100, at=fleet.now - 5 + index / 100)
        self.assertIsNone(fleet.confirm())
        self.assertEqual(fleet.scheduler._last_decision["evidence_blocked_gate"], "trend")

    def test_floor_diagnostic_retains_lower_objective_candidate(self) -> None:
        fleet = self.fleet
        fleet.scheduler.min_decode = 5
        self.assertIsNone(fleet.confirm())
        decision = fleet.scheduler._last_decision
        self.assertEqual(decision["reason"], "min_decode blocks the adjacent split")
        self.assertEqual(decision["prefill"], 2)
        self.assertGreater(decision["objective_delta"], 0)

    def test_prefill_floor_blocks_reverse_move(self) -> None:
        fleet = self.fleet
        self.assertIsNotNone(fleet.confirm())
        fleet.scheduler.min_prefill = 2
        fleet.pressure = {Role.PREFILL: 0.1, Role.DECODE: 12.0}
        self.assertIsNone(fleet.step())
        self.assertEqual(
            fleet.scheduler._last_decision["reason"], "min_prefill blocks the adjacent split"
        )

    def test_pins_and_dwell_expose_specific_refusal(self) -> None:
        fleet = self.fleet
        fleet.scheduler.pinned = frozenset(f"e{i}" for i in range(1, 6))
        self.assertIsNone(fleet.confirm())
        self.assertIn("pins", fleet.scheduler._last_decision["reason"])
        fleet.scheduler.pinned = frozenset()
        fleet.scheduler._last_flip = {f"e{i}": fleet.now for i in range(1, 6)}
        self.assertIsNone(fleet.confirm())
        self.assertEqual(
            fleet.scheduler._last_decision["reason"], "source engine dwell has not elapsed"
        )
        self.assertIsNotNone(fleet.confirm())

    def test_decode_recovery_still_obeys_cooldown(self) -> None:
        fleet = self.fleet
        self.assertIsNotNone(fleet.confirm())
        fleet.pressure = {Role.PREFILL: 0.1, Role.DECODE: 12.0}
        fleet.scheduler._last_p2d_flip = fleet.now
        self.assertIsNone(fleet.step())
        self.assertEqual(
            fleet.scheduler._last_decision["reason"], "decode cooldown has not elapsed"
        )
        self.assertIsNotNone(fleet.step())
        self.assertEqual(fleet.scheduler.flips[-1].to, Role.DECODE)

    def test_advisory_and_unavailable_fleet_cannot_mutate(self) -> None:
        fleet = self.fleet
        fleet.scheduler.advisory = True
        self.assertIsNone(fleet.confirm())
        self.assertEqual(fleet.scheduler._last_decision["result"], "advisory")
        self.assertEqual(fleet.scheduler.flips, [])
        fleet.scheduler.advisory = False
        fleet.scheduler.drain("e5")
        self.assertIsNone(fleet.confirm())
        self.assertEqual(fleet.scheduler._last_decision["reason"], "fleet health is changing")


class ProjectedTTFTRecoveryTests(unittest.TestCase):
    """Incoming prefill risk wakes one guarded adjacent D-to-P evaluation."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.fleet = Fleet(directory.name)
        self.addCleanup(self.fleet.telemetry.stop)
        self.fleet.pressure = {Role.PREFILL: 0.1, Role.DECODE: 0.4}
        self.assertIsNone(self.fleet.controller.step())

    def queue(self, count: int, *, input_len: int = 100) -> list[Request]:
        """Publish FIFO prefill work at the current monotonic time."""
        requests = []
        for index in range(count):
            request = Request(
                f"urgent-{index}",
                input_len,
                wanted_len=10,
                arrived_at=self.fleet.now,
            )
            self.fleet.monitor.waiting[request.rid] = request
            requests.append(request)
        return requests

    def test_offer_wakes_before_cadence_and_applies_one_adjacent_move(self) -> None:
        fleet = self.fleet
        last_step = fleet.controller._last_step
        requests = self.queue(11)
        fleet.now += 0.1
        self.assertTrue(fleet.controller.note_prefill_risk(requests[-1]))
        moved = fleet.controller.step(urgent=True)
        self.assertIsNotNone(moved)
        self.assertLess(fleet.now - last_step, fleet.controller.step_s)
        self.assertEqual(len(fleet.monitor.pool(Role.PREFILL)), 2)
        decision = fleet.scheduler._last_decision
        self.assertEqual(decision["eligibility_rule"], "projected_ttft_recovery")
        self.assertLess(decision["event_to_evaluation_s"], 0.8)
        self.assertGreater(decision["projected_ttft_s"], fleet.scheduler.slo.ttft_s)
        self.assertGreater(decision["projected_ttft_improvement_s"], 0)

    def test_concurrent_risky_offers_coalesce_into_one_evaluation(self) -> None:
        fleet = self.fleet
        requests = self.queue(12)
        self.assertTrue(fleet.controller.note_prefill_risk(requests[-2]))
        self.assertTrue(fleet.controller.note_prefill_risk(requests[-1]))
        self.assertIsNotNone(fleet.controller.step(urgent=True))
        self.assertEqual(len(fleet.scheduler.flips), 1)
        self.assertEqual(fleet.scheduler._last_decision["urgent_signals"], 2)

    def test_drained_queue_clears_the_coalesced_event(self) -> None:
        fleet = self.fleet
        requests = self.queue(11)
        self.assertTrue(fleet.controller.note_prefill_risk(requests[-1]))
        fleet.monitor.waiting.clear()
        self.assertIsNone(fleet.controller.step(urgent=True))
        self.assertEqual(fleet.scheduler.flips, [])
        self.assertIsNone(fleet.controller._prefill_urgency)

    def test_new_control_epoch_clears_urgent_state(self) -> None:
        fleet = self.fleet
        request = self.queue(11)[-1]
        self.assertTrue(fleet.controller.note_prefill_risk(request))
        fleet.controller.clear_prefill_risk()
        self.assertIsNone(fleet.controller._prefill_urgency)
        self.assertIsNone(fleet.controller.step(urgent=True))

    def test_isolated_large_prompt_records_zero_candidate_gain(self) -> None:
        fleet = self.fleet
        request = self.queue(1, input_len=1100)[0]
        self.assertTrue(fleet.controller.note_prefill_risk(request))
        self.assertIsNone(fleet.controller.step(urgent=True))
        decision = fleet.scheduler._last_decision
        self.assertEqual(
            decision["reason"],
            "projected TTFT recovery requires a strict prefill improvement",
        )
        self.assertEqual(decision["projected_ttft_improvement_s"], 0.0)

    def test_persistent_risk_progresses_through_separate_adjacent_moves(self) -> None:
        fleet = self.fleet
        requests = self.queue(25)
        self.assertTrue(fleet.controller.note_prefill_risk(requests[-1]))
        self.assertIsNotNone(fleet.controller.step(urgent=True))
        self.assertEqual(len(fleet.monitor.pool(Role.PREFILL)), 2)
        self.assertTrue(fleet.controller.prefill_risk_pending)
        self.assertIsNotNone(fleet.controller.step(urgent=True))
        self.assertEqual(len(fleet.monitor.pool(Role.PREFILL)), 3)
        self.assertEqual([flip.to for flip in fleet.scheduler.flips], [Role.PREFILL] * 2)

    def test_urgent_move_retains_profile_capacity_evidence_and_floor_guards(self) -> None:
        fleet = self.fleet
        requests = self.queue(11)
        fleet.profiles._by_id.pop("e5")
        self.assertTrue(fleet.controller.note_prefill_risk(requests[-1]))
        self.assertIsNone(fleet.controller.step(urgent=True))
        self.assertEqual(
            fleet.scheduler._last_decision["reason"],
            "projected TTFT recovery requires fleet profiles",
        )

        fleet.profiles.put(
            Profile(
                "e5",
                0.0,
                0.001,
                0.0,
                0.000001,
                0.001,
                kv_capacity_tokens=100_000,
                decode_min_requests=1,
                decode_max_requests=100,
                decode_min_kv_tokens=1,
                decode_max_kv_tokens=100_000,
                decode_fit_mape=0.0,
                decode_cv_mape=0.0,
            )
        )
        fleet.scheduler.min_decode = 5
        self.assertTrue(fleet.controller.note_prefill_risk(requests[-1]))
        self.assertIsNone(fleet.controller.step(urgent=True))
        self.assertEqual(
            fleet.scheduler._last_decision["reason"], "min_decode blocks the adjacent split"
        )

        fleet.scheduler.min_decode = 1
        fleet.controller.safety.note_risk_event("first_token_timeout")
        self.assertTrue(fleet.controller.note_prefill_risk(requests[-1]))
        self.assertIsNone(fleet.controller.step(urgent=True))
        self.assertEqual(fleet.scheduler._last_decision["evidence_blocked_gate"], "risk")

    def test_urgent_move_retains_decode_capacity_guard(self) -> None:
        fleet = self.fleet
        requests = self.queue(11)
        requests[0].wanted_len = 1_000_000
        self.assertTrue(fleet.controller.note_prefill_risk(requests[-1]))
        self.assertIsNone(fleet.controller.step(urgent=True))
        decision = fleet.scheduler._last_decision
        self.assertEqual(decision["reason"], "decode profile does not cover proposed work")
        self.assertGreater(decision["pending_decode_tokens"], decision["decode_kv_capacity_tokens"])

    def test_urgent_move_retains_pin_dwell_and_resident_guards(self) -> None:
        fleet = self.fleet
        requests = self.queue(11)
        fleet.scheduler.pinned = frozenset(f"e{index}" for index in range(1, 6))
        self.assertTrue(fleet.controller.note_prefill_risk(requests[-1]))
        self.assertIsNone(fleet.controller.step(urgent=True))
        self.assertIn("pins", fleet.scheduler._last_decision["reason"])

        fleet.scheduler.pinned = frozenset()
        fleet.scheduler._last_flip = {f"e{index}": fleet.now for index in range(1, 6)}
        self.assertTrue(fleet.controller.note_prefill_risk(requests[-1]))
        self.assertIsNone(fleet.controller.step(urgent=True))
        self.assertEqual(
            fleet.scheduler._last_decision["reason"], "source engine dwell has not elapsed"
        )

        fleet.scheduler._last_flip.clear()
        fleet.scheduler.th.flip_resident_guard = 2
        for iid in ("e1", "e2", "e3", "e4", "e5"):
            for index in range(3):
                fleet.monitor.dispatched(
                    iid,
                    Request(f"resident-{iid}-{index}", 100, phase=Phase.DECODE),
                )
        self.assertTrue(fleet.controller.note_prefill_risk(requests[-1]))
        self.assertIsNone(fleet.controller.step(urgent=True))
        self.assertIn("flip_resident_guard", fleet.scheduler._last_decision["reason"])

    def test_urgent_move_holds_while_fleet_health_changes(self) -> None:
        fleet = self.fleet
        requests = self.queue(11)
        fleet.scheduler.drain("e5")
        self.assertTrue(fleet.controller.note_prefill_risk(requests[-1]))
        self.assertIsNone(fleet.controller.step(urgent=True))
        self.assertEqual(fleet.scheduler._last_decision["reason"], "fleet health is changing")


class OccupiedTransitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_resident_completion_and_cancellation_keep_original_decode(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        dispatches = []

        def engine(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            phase = (
                "prefill"
                if body.get("kv_transfer_params", {}).get("do_remote_decode")
                else "decode"
            )
            dispatches.append((request.url.host, phase, body["prompt"]))
            if phase == "prefill":
                return httpx.Response(
                    200,
                    json={
                        "kv_transfer_params": {
                            "remote_engine_id": request.url.host,
                            "remote_block_ids": [1],
                            "remote_host": "stub",
                            "remote_port": 0,
                        }
                    },
                )
            chunks = [
                "data: " + json.dumps({"choices": [{"text": str(index), "token_ids": [index]}]})
                for index in range(3)
            ]
            return httpx.Response(200, text="\n\n".join([*chunks, "data: [DONE]", ""]))

        fleet = Fleet(directory.name, httpx.MockTransport(engine))
        self.addCleanup(fleet.telemetry.stop)
        self.addCleanup(fleet.journal.close)
        self.addAsyncCleanup(fleet.router.engines.aclose)
        router = fleet.router
        streams = []

        async def request(prompt):
            response = await router.serve(
                "/v1/completions",
                {
                    "prompt": prompt,
                    "max_tokens": 3,
                    "stream": True,
                },
                {},
            )
            self.assertIsInstance(response, RequestStreamResponse)
            self.addAsyncCleanup(response.aclose)
            streams.append(response)
            return response

        # Placement prefers e1's measured costs. Pins constrain role changes
        # only, leaving e1 as the occupied source candidate for the policy.
        fleet.scheduler.pinned = frozenset(f"e{i}" for i in range(2, 6))
        fleet.profiles.put(replace(fleet.profiles.get("e1"), tpot_intercept=0.0001))
        for index in range(5):
            response = await request(f"old{index}")
            frame = await anext(response.body_iterator)
            self.assertEqual(json.loads(frame.removeprefix("data: "))["choices"][0]["text"], "0")
        first, cancelled = streams[0], streams[1]
        self.assertEqual(first.lifecycle.decode_iid, "e1")
        self.assertEqual(cancelled.lifecycle.decode_iid, "e1")
        self.assertIsNone(fleet.step())
        self.assertIsNone(fleet.step())
        moved = fleet.step()
        self.assertIsNotNone(moved, fleet.scheduler._last_decision)
        self.assertEqual(moved.iid, "e1")
        flip = fleet.scheduler.flips[-1]
        self.assertEqual(
            flip.resident_ids, frozenset(response.lifecycle.rid for response in streams)
        )
        self.assertIsNone(flip.drained_s)
        self.assertIs(
            fleet.monitor.instances["e1"].decode[first.lifecycle.rid], first.lifecycle.request
        )
        # Hold e0's prefill slot so a fresh original selects e1 in its new role.
        fleet.monitor.dispatched("e0", Request("busy", 100))
        fresh = await request("fresh")
        self.assertEqual(fresh.lifecycle.prefill_iid, "e1")
        self.assertNotEqual(fresh.lifecycle.decode_iid, "e1")
        self.assertEqual(len(fleet.scheduler.flips), 1)
        frame = await anext(first.body_iterator)
        self.assertEqual(json.loads(frame.removeprefix("data: "))["choices"][0]["text"], "1")
        self.assertEqual(first.lifecycle.decode_iid, "e1")
        fleet.scheduler.settle_drains()
        self.assertIsNone(flip.drained_s)
        # Cancellation and successful completion both release exactly once.
        await cancelled.aclose()
        await cancelled.aclose()
        fleet.scheduler.settle_drains()
        self.assertIsNone(flip.drained_s)
        self.assertEqual(cancelled.lifecycle.decode_iid, "e1")
        self.assertEqual(set(moved.decode), flip.resident_ids - {cancelled.lifecycle.rid})
        fleet.advance(1)
        for response in streams:
            if response is not cancelled:
                remaining = [chunk async for chunk in response.body_iterator]
                self.assertEqual(sum("[DONE]" in chunk for chunk in remaining), 1)
                await response.aclose()
                self.assertEqual(response.lifecycle.tokens, 3)
        fleet.monitor.finished("e0", "busy")
        fleet.scheduler.settle_drains()
        self.assertEqual(flip.drained_s, 1.0)
        self.assertEqual(
            (router.offered, router.served, router.cancelled, router.failed), (6, 5, 1, 0)
        )
        self.assertEqual(router.inflight, 0)
        self.assertEqual(router._seat_since, {})
        self.assertTrue(
            all(not i.decode and not i.prefill for i in fleet.monitor.instances.values())
        )
        rows = [json.loads(line) for line in fleet.journal.path.read_text().splitlines()]
        terminal = [row for row in rows if "terminal" in row]
        self.assertEqual(len(terminal), 6)
        self.assertEqual(len({row["rid"] for row in terminal}), 6)
        self.assertTrue(
            all(row["decode_iid"] == "e1" for row in terminal if row["rid"] in flip.resident_ids)
        )
        self.assertEqual({row["terminal"] for row in terminal}, {"completed", "cancelled"})
        self.assertTrue(all(row["attempts"] == row["decode_attempts"] == 1 for row in terminal))
        self.assertEqual(
            [row["decode_iid"] for row in terminal if row["rid"] == first.lifecycle.rid], ["e1"]
        )
        self.assertEqual(len(dispatches), 12)
