"""Recent-demand donor pricing after an abrupt workload change."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from narwhal.profiling.store import ProfileStore
from narwhal.scheduling.control import SLO, Thresholds
from narwhal.scheduling.controller import ReactiveController
from narwhal.scheduling.monitor import InstanceMonitor
from narwhal.scheduling.scheduler import GlobalScheduler
from narwhal.types import Instance, Role
from tests.fixtures import profile

PREFILL_HEAVY = (8192, 16, 13.5)
DECODE_HEAVY = (256, 1200, 5.0)


class RecentDemandTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.now = 0.0
        store = ProfileStore(Path(directory.name) / "profiles.json")
        self.monitor = InstanceMonitor(clock=lambda: self.now, profiles=store)
        for index in range(8):
            iid = f"e{index}"
            store.put(
                profile(
                    iid,
                    ttft_a=0.0,
                    ttft_b=0.355 / 8192,
                    ttft_c=0.0,
                    tpot_slope=1e-9,
                    tpot_intercept=0.008,
                    tpot_request_slope=0.0003,
                    kv_capacity_tokens=10_000_000,
                    decode_max_requests=256,
                    decode_max_kv_tokens=10_000_000,
                )
            )
            role = Role.PREFILL if index < 7 else Role.DECODE
            self.monitor.add(Instance(iid, f"http://{iid}", role))
        self.events: list[dict] = []
        self.scheduler = GlobalScheduler(
            self.monitor,
            store,
            SLO(2.1, 0.045),
            Thresholds(),
            clock=lambda: self.now,
            decode_concurrency=16,
            on_control_event=self.events.append,
        )
        self.controller = ReactiveController(
            self.monitor,
            self.scheduler,
            clock=lambda: self.now,
            window_s=120.0,
            confirmations=2,
            utilization=0.8,
            min_arrivals=10,
            demand_floor=0.5,
        )
        telemetry = patch.object(self.scheduler, "pool_load", return_value=0.0)
        telemetry.start()
        self.addCleanup(telemetry.stop)
        self.carry = 0.0

    def run_for(
        self, seconds: int, shape: tuple[int, int, float], role: Role = Role.DECODE
    ) -> list[float]:
        """Offer one workload for `seconds` and return the times of applied moves to `role`."""
        input_len, output_len, rate = shape
        moves = []
        for _ in range(seconds):
            self.carry += rate
            while self.carry >= 1.0:
                self.carry -= 1.0
                self.controller.saw_arrival(input_len, wanted_len=output_len, at=self.now)
            self.now += 1.0
            self.controller.sample()
            moved = self.controller.step()
            if moved is not None and moved.role is role:
                moves.append(self.now)
        return moves

    def test_decode_phase_takes_a_prefill_engine_once_both_horizons_need_it(self) -> None:
        self.run_for(200, PREFILL_HEAVY)
        self.assertEqual(len(self.monitor.pool(Role.PREFILL)), 7)
        flip_at = self.now
        moves = self.run_for(60, DECODE_HEAVY)
        self.assertTrue(moves)
        self.assertLessEqual(moves[0] - flip_at, 30.0)
        decision = next(
            row
            for row in self.events
            if row["event"] == "controller_decision"
            and row["result"] == "applied"
            and row["at"] == moves[0]
        )
        self.assertEqual(decision["eligibility_rule"], "recent_demand")
        self.assertEqual(decision["recent_window_s"], 30.0)

    def test_short_receiver_burst_leaves_the_window_rule_in_charge(self) -> None:
        self.run_for(200, PREFILL_HEAVY)
        moves = self.run_for(15, DECODE_HEAVY)
        moves += self.run_for(45, PREFILL_HEAVY)
        self.assertEqual(moves, [])
        self.assertEqual(len(self.monitor.pool(Role.PREFILL)), 7)

    def test_move_back_waits_for_fresh_evidence_after_a_decode_move(self) -> None:
        self.run_for(200, PREFILL_HEAVY)
        last = self.run_for(120, DECODE_HEAVY)[-1]
        quiet = int(last + 55.0 - self.now)
        self.assertEqual(self.run_for(quiet, PREFILL_HEAVY, role=Role.PREFILL), [])
        held = [
            row
            for row in self.events
            if row["event"] == "controller_decision"
            and row["at"] > last
            and row.get("eligibility_rule") == "recent_demand"
            and row.get("evidence_blocked_gate") == "risk"
        ]
        self.assertTrue(held)
        back = self.run_for(10, PREFILL_HEAVY, role=Role.PREFILL)
        self.assertTrue(back)
        self.assertGreaterEqual(back[0] - last, 60.0)
