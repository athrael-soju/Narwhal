"""Steady-load consolidation compares adjacent splits on their projected objective."""

from __future__ import annotations

import heapq
import itertools
import tempfile
import unittest

from narwhal.scheduling.reactive import Departure
from narwhal.serving.schemas import ControllerDecisionOut
from narwhal.types import Phase, Request, Role
from tests.scheduling.test_departure import Sim
from tests.scheduling.test_reactive import Fleet

# Full-slot token interval of the Sim decode profile at 16 requests.
TOKEN_S = 0.008 + 0.0003 * 16


class HandoffSim(Sim):
    """Requests wait on their prefill engine, then decode on the least-loaded decode engine."""

    def __init__(self, directory: str) -> None:
        super().__init__(directory, prefill=6)
        self.free_at = dict.fromkeys(self.monitor.instances, 0.0)
        self.queue: list[tuple[float, int, str, Request]] = []
        self.ids = itertools.count()

    def run(self, seconds: int, workload: tuple[int, int, float]) -> None:
        input_len, output_len, rate = workload
        for _ in range(seconds):
            tick = self.now
            self.carry += rate
            count = int(self.carry)
            self.carry -= count
            for k in range(count):
                self.arrive(tick + (k + 1) / (count + 1), input_len, output_len)
            self.complete(tick + 1.0)
            self.now = tick + 1.0
            self.controller.sample()
            self.controller.step()

    def arrive(self, at: float, input_len: int, output_len: int) -> None:
        self.complete(at)
        self.now = at
        self.controller.saw_arrival(input_len, wanted_len=output_len, at=at)
        engine = min(
            self.scheduler.live_instances(Role.PREFILL),
            key=lambda inst: (self.free_at[inst.iid], inst.iid),
        )
        done = max(self.free_at[engine.iid], at) + self.scheduler.profiles.get(
            engine.iid
        ).prefill_time(input_len)
        self.free_at[engine.iid] = done
        order = next(self.ids)
        request = Request(f"r{order}", input_len, wanted_len=output_len, arrived_at=at)
        self.monitor.dispatched(engine.iid, request)
        heapq.heappush(self.queue, (done, order, engine.iid, request))

    def complete(self, until: float) -> None:
        while self.queue and self.queue[0][0] <= until:
            at, order, iid, request = heapq.heappop(self.queue)
            self.now = at
            self.monitor.finished(iid, request.rid)
            if request.phase is Phase.PREFILL:
                request.phase = Phase.DECODE
                engine = min(
                    self.scheduler.live_instances(Role.DECODE),
                    key=lambda inst: (len(inst.decode), inst.iid),
                )
                self.monitor.dispatched(engine.iid, request)
                done = at + request.wanted_len * TOKEN_S
                heapq.heappush(self.queue, (done, order, engine.iid, request))


class SteadyDemandTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.fleet = Fleet(directory.name)
        self.addCleanup(self.fleet.telemetry.stop)
        # Prefill below expand keeps mixed pressure off; the 2P4D decode source is 0.75.
        self.fleet.pressure = {Role.PREFILL: 0.9, Role.DECODE: 3.0}

    def run_steps(self, count: int) -> list[dict[str, object]]:
        rows = []
        for _ in range(count):
            self.fleet.step()
            rows.append(dict(self.fleet.scheduler._last_decision))
        return rows

    def test_steady_light_decode_consolidates_above_shrink(self) -> None:
        fleet = self.fleet
        rows = self.run_steps(12)
        self.assertEqual(rows[0]["reason"], "projected decode pressure blocks consolidation")
        self.assertEqual(rows[0]["eligibility_rule"], "source_shrink")
        self.assertEqual(fleet.scheduler.flips, [])

        rows += self.run_steps(4)

        applied = [row for row in rows if row["result"] == "applied"]
        self.assertEqual(len(applied), 1)
        row = applied[0]
        self.assertEqual((row["current_prefill"], row["prefill"]), (1, 2))
        self.assertEqual(row["eligibility_rule"], "steady_demand")
        self.assertEqual(row["required_confirmations"], 3)
        self.assertGreater(row["projected_tpot_ratio"], fleet.scheduler.th.shrink)
        self.assertLessEqual(row["projected_tpot_ratio"], fleet.scheduler.th.expand)
        self.assertGreaterEqual(row["steady_demand_s"], fleet.controller.safety.evidence_span_s)
        self.assertEqual(row["demand_horizon_s"], fleet.controller.window_s)
        self.assertEqual(row["steady_horizon_s"], 15.0)
        ControllerDecisionOut.model_validate(row)

    def test_steady_load_keeps_the_better_split(self) -> None:
        fleet = self.fleet
        self.run_steps(16)
        self.assertEqual(len(fleet.monitor.pool(Role.PREFILL)), 2)

        policy = fleet.controller.reactive
        for _ in range(48):
            self.run_steps(1)
            self.assertIsNone(policy.departure)

        self.assertGreaterEqual(fleet.now - policy.settled_since, fleet.controller.window_s / 2)
        self.assertEqual(len(fleet.scheduler.flips), 1)
        self.assertEqual(fleet.scheduler.control_snapshot()["flip_reversals"], 0)

    def test_decode_source_above_expand_holds(self) -> None:
        fleet = self.fleet
        fleet.pressure[Role.DECODE] = 4.5

        rows = self.run_steps(24)

        self.assertEqual(fleet.scheduler.flips, [])
        self.assertEqual(rows[-1]["reason"], "projected decode pressure blocks consolidation")
        self.assertGreater(rows[-1]["projected_tpot_ratio"], fleet.scheduler.th.expand)
        self.assertGreaterEqual(rows[-1]["steady_demand_s"], 60.0)

    def test_rise_within_the_tolerance_of_the_larger_demand_keeps_the_steady_span(self) -> None:
        fleet = self.fleet
        before = self.run_steps(8)[-1]["steady_demand_s"]
        for _ in range(15):
            fleet.controller.saw_arrival(135, wanted_len=10, at=fleet.now)
            fleet.now += 1.0
            fleet.controller.step()
        row = fleet.scheduler._last_decision
        self.assertGreater(row["steady_prefill_work"], 1.25 * row["prefill_work"])
        self.assertEqual(row["steady_demand_s"], before + 15.0)

    def test_shifting_demand_restarts_the_steady_span(self) -> None:
        fleet = self.fleet
        self.run_steps(8)
        for _ in range(15):
            fleet.controller.saw_arrival(4000, wanted_len=10, at=fleet.now)
            fleet.now += 1.0
            fleet.controller.step()
        self.assertIsNone(fleet.scheduler._last_decision["steady_demand_s"])

        rows = self.run_steps(10)

        self.assertEqual(fleet.scheduler.flips, [])
        self.assertTrue(all(row["eligibility_rule"] == "source_shrink" for row in rows))
        ControllerDecisionOut.model_validate(rows[-1])

    def urgent_wake(self) -> dict[str, object]:
        fleet = self.fleet
        request = Request("long", 1100, wanted_len=10, arrived_at=fleet.now)
        fleet.monitor.waiting[request.rid] = request
        self.assertTrue(fleet.controller.note_prefill_risk(request))
        fleet.controller.step(urgent=True)
        fleet.monitor.waiting.pop(request.rid)
        return dict(fleet.scheduler._last_decision)

    def test_held_urgent_wake_keeps_the_steady_span(self) -> None:
        rows = self.run_steps(10)
        row = self.urgent_wake()
        self.assertEqual(
            (row["result"], row["eligibility_rule"]), ("held", "projected_ttft_recovery")
        )
        self.assertIsNone(row["steady_demand_s"])
        self.assertIsNone(row["steady_prefill_work"])

        rows += self.run_steps(6)

        applied = [row for row in rows if row["result"] == "applied"]
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0]["eligibility_rule"], "steady_demand")
        self.assertEqual(applied[0]["steady_demand_s"], 70.0)
        self.assertEqual(rows.index(applied[0]), 14)

    def test_held_urgent_wake_keeps_an_open_departure(self) -> None:
        policy = self.fleet.controller.reactive
        self.run_steps(10)
        departure = Departure(1, self.fleet.now)
        policy.departure = departure
        runs = (policy.settled_since, policy.balanced_since)

        self.assertEqual(self.urgent_wake()["result"], "held")

        self.assertIs(policy.departure, departure)
        self.assertEqual((policy.settled_since, policy.balanced_since), runs)

    def test_evaluation_gap_restarts_the_settled_and_steady_runs(self) -> None:
        fleet = self.fleet
        policy = fleet.controller.reactive
        self.run_steps(24)
        self.assertEqual(len(fleet.scheduler.flips), 1)
        settled_since = policy.settled_since
        steady_s = fleet.scheduler._last_decision["steady_demand_s"]

        # A cadence step one second late keeps both runs.
        fleet.advance(6)
        fleet.controller.step()
        self.assertEqual(policy.settled_since, settled_since)
        self.assertEqual(fleet.scheduler._last_decision["steady_demand_s"], steady_s + 6.0)

        # A role freeze stops evaluation for a minute.
        fleet.advance(60)
        fleet.controller.step()
        self.assertEqual(policy.settled_since, fleet.now)
        self.assertEqual(fleet.scheduler._last_decision["steady_demand_s"], 0.0)


class SlotHeadroomTests(unittest.TestCase):
    """Steady consolidation keeps decode slots for requests reaching decode."""

    def run_load(self, output_len: int, seconds: int) -> HandoffSim:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        sim = HandoffSim(directory.name)
        self.addCleanup(sim.close)
        sim.run(seconds, (8192, output_len, 15.0))
        return sim

    def test_requests_in_prefill_count_within_one_decode_residency(self) -> None:
        # 5 requests finish prefill at 0.355 s.
        # 64 tokens decode for 0.83 s, the last 24 for 0.31 s and 8 tokens for 0.1 s.
        cases = ((64, 0, 12, 17), (64, 40, 12, 12), (8, 0, 2, 2))
        for output_len, delivered, residents, peak in cases:
            with self.subTest(output_len=output_len, delivered=delivered):
                sim = self.run_load(output_len, 20)
                for inst in sim.monitor.instances.values():
                    for request in inst.decode.values():
                        request.output_len = delivered
                snapshot = sim.controller.scorer.capture(
                    sim.now, sim.controller.last_demand, utilization=0.8, observed_load=(0.0, 0.0)
                )
                self.assertEqual(snapshot.decode_role_requests, residents)
                self.assertEqual(snapshot.pending_decode_requests, 5)
                self.assertEqual(sim.controller.scorer.decode_peak_requests(), peak)

    def test_prefill_bound_load_holds_when_slot_load_exceeds_one_decode_engine(self) -> None:
        sim = self.run_load(64, 150)
        th = sim.scheduler.th

        self.assertEqual(sim.scheduler.flips, [])
        row = sim.decisions[-1]
        self.assertEqual((row["current_prefill"], row["prefill"], row["result"]), (6, 7, "held"))
        self.assertEqual(row["reason"], "projected decode pressure blocks consolidation")
        self.assertEqual(row["eligibility_rule"], "source_shrink")
        self.assertEqual(row["evidence_blocked_gate"], "none")
        self.assertGreaterEqual(row["steady_demand_s"], sim.controller.safety.evidence_span_s)
        self.assertGreater(row["projected_tpot_ratio"], th.shrink)
        self.assertLessEqual(row["projected_tpot_ratio"], th.expand)
        self.assertGreaterEqual(row["objective_delta"], sim.controller.movement_margin)


if __name__ == "__main__":
    unittest.main()
