"""Check departures from a settled split after a sustained demand shift."""

from __future__ import annotations

import random
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from narwhal.profiling.store import ProfileStore
from narwhal.scheduling.control import SLO, Thresholds
from narwhal.scheduling.controller import ReactiveController
from narwhal.scheduling.demand import Demand
from narwhal.scheduling.monitor import InstanceMonitor
from narwhal.scheduling.reactive import Departure, ReactivePolicy, ShortView
from narwhal.scheduling.scheduler.placement import GlobalScheduler
from narwhal.scheduling.scoring import SplitScore
from narwhal.serving.schemas import ControllerDecisionOut
from narwhal.types import Instance, Role
from tests.fixtures import profile

PREFILL_HEAVY = (8192, 16, 13.5)
DECODE_HEAVY = (256, 1200, 5.0)
DECODE_LEANING = (2048, 128, 10.0)


def score(prefill: int, objective: float) -> SplitScore:
    return SplitScore(prefill, 8 - prefill, objective, objective, objective)


class TrackDepartureTests(unittest.TestCase):
    """A departure opens only after a settled run and a demand shift."""

    def setUp(self) -> None:
        self.controller = SimpleNamespace(
            movement_margin=0.05,
            window_s=120.0,
            step_s=5.0,
            within_floors=lambda p: 0 < p < 8,
            safety=SimpleNamespace(demand_rise_tolerance=0.25, evidence_span_s=60.0),
        )
        self.policy = ReactivePolicy()
        self.window = Demand(4.8, 0.2, 100, 0)

    def track(
        self,
        now: float,
        *,
        short_gain: float,
        short_demand: Demand,
        current_p: int = 7,
        toward: int | None = None,
    ) -> None:
        current = score(current_p, 1.0)
        best = current_p - 1 if toward is None else toward
        short = ShortView(short_demand, current, {best: score(best, 1.0 - short_gain)})
        self.policy._track_departure(
            self.controller, now, current, [score(6, 1.1)], self.window, short
        )

    def settle(self, until: float) -> None:
        for now in range(0, int(until) + 1, 5):
            self.track(float(now), short_gain=0.0, short_demand=self.window)

    def test_shift_after_one_evidence_span_settled_opens_a_departure(self):
        self.settle(60.0)
        self.track(65.0, short_gain=0.5, short_demand=Demand(2.4, 2.5, 100, 0))
        self.assertEqual(self.policy.departure, Departure(-1, 65.0))

    def test_the_settled_run_lasts_one_evidence_span(self):
        self.controller.safety.evidence_span_s = 40.0
        for settled, departure in ((30.0, None), (40.0, Departure(-1, 45.0))):
            with self.subTest(settled=settled):
                self.policy = ReactivePolicy()
                self.settle(settled)
                self.track(settled + 5.0, short_gain=0.5, short_demand=Demand(2.4, 2.5, 100, 0))
                self.assertEqual(self.policy.departure, departure)

    def test_shorter_settled_run_opens_no_departure(self):
        self.settle(50.0)
        self.track(55.0, short_gain=0.5, short_demand=Demand(2.4, 2.5, 100, 0))
        self.assertIsNone(self.policy.departure)

    def test_short_demand_within_the_rise_tolerance_opens_no_departure(self):
        self.settle(60.0)
        self.track(65.0, short_gain=0.06, short_demand=Demand(4.5, 0.22, 100, 0))
        self.assertIsNone(self.policy.departure)

    def test_rise_within_the_tolerance_of_the_larger_demand_opens_no_departure(self):
        self.settle(60.0)
        self.track(65.0, short_gain=0.5, short_demand=Demand(4.8, 0.26, 100, 0))
        self.assertIsNone(self.policy.departure)

    def test_rise_beyond_the_tolerance_of_the_larger_demand_opens_a_departure(self):
        self.settle(60.0)
        self.track(65.0, short_gain=0.5, short_demand=Demand(4.8, 0.28, 100, 0))
        self.assertEqual(self.policy.departure, Departure(-1, 65.0))

    def test_one_unsettled_evaluation_keeps_the_settled_run(self):
        self.settle(55.0)
        self.track(57.0, short_gain=0.5, short_demand=self.window)
        self.track(60.0, short_gain=0.0, short_demand=self.window)
        self.track(65.0, short_gain=0.5, short_demand=Demand(2.4, 2.5, 100, 0))
        self.assertEqual(self.policy.departure, Departure(-1, 65.0))

    def test_unsettled_evaluations_for_one_step_restart_the_settled_run(self):
        self.settle(45.0)
        self.track(50.0, short_gain=0.5, short_demand=self.window)
        self.track(55.0, short_gain=0.5, short_demand=self.window)
        self.track(60.0, short_gain=0.0, short_demand=self.window)
        self.track(65.0, short_gain=0.5, short_demand=Demand(2.4, 2.5, 100, 0))
        self.assertIsNone(self.policy.departure)

    def test_departure_closes_when_the_short_view_turns(self):
        self.settle(60.0)
        self.track(65.0, short_gain=0.5, short_demand=Demand(2.4, 2.5, 100, 0))
        self.track(70.0, short_gain=0.0, short_demand=Demand(4.8, 0.2, 100, 0))
        self.assertIsNone(self.policy.departure)

    def moved(self) -> None:
        self.settle(60.0)
        self.track(65.0, short_gain=0.5, short_demand=Demand(2.4, 2.5, 100, 0))
        self.policy.departure = replace(self.policy.departure, moved=True)

    def test_moved_departure_leads_while_the_shift_lasts(self):
        self.moved()
        self.track(70.0, short_gain=0.5, short_demand=Demand(2.4, 2.5, 100, 0))
        self.assertEqual(self.policy.departure, Departure(-1, 65.0, moved=True, leading=True))

    def test_moved_departure_leads_through_one_evaluation_without_the_shift(self):
        self.moved()
        self.track(70.0, short_gain=0.5, short_demand=self.window)
        self.track(75.0, short_gain=0.5, short_demand=Demand(2.4, 2.5, 100, 0))
        self.assertEqual(self.policy.departure, Departure(-1, 65.0, moved=True, leading=True))

    def test_moved_departure_stops_leading_when_the_shift_ends_for_one_step(self):
        self.moved()
        self.track(70.0, short_gain=0.5, short_demand=self.window)
        self.track(75.0, short_gain=0.5, short_demand=self.window)
        self.assertEqual(self.policy.departure, Departure(-1, 65.0, moved=True, leading=False))

    def test_moved_departure_leads_through_an_evaluation_below_the_margin(self):
        self.moved()
        self.track(70.0, short_gain=0.0, short_demand=Demand(2.4, 2.5, 100, 0), current_p=6)
        self.track(75.0, short_gain=0.5, short_demand=Demand(2.4, 2.5, 100, 0), current_p=6)
        self.assertEqual(self.policy.departure, Departure(-1, 65.0, moved=True, leading=True))

    def test_moved_departure_leads_through_one_evaluation_pointing_back(self):
        self.moved()
        self.track(
            70.0, short_gain=0.5, short_demand=Demand(2.4, 2.5, 100, 0), current_p=6, toward=7
        )
        self.assertEqual(self.policy.departure, Departure(-1, 65.0, moved=True, leading=True))

    def test_moved_departure_stops_leading_when_the_short_view_points_back_for_one_step(self):
        self.moved()
        for now in (70.0, 75.0):
            self.track(
                now, short_gain=0.5, short_demand=Demand(2.4, 2.5, 100, 0), current_p=6, toward=7
            )
        self.assertEqual(self.policy.departure, Departure(-1, 65.0, moved=True, leading=False))

    def test_moved_departure_closes_one_window_after_it_opens(self):
        self.moved()
        self.track(180.0, short_gain=0.5, short_demand=Demand(2.4, 2.5, 100, 0))
        self.assertEqual(self.policy.departure.started_at, 65.0)
        self.track(185.0, short_gain=0.5, short_demand=Demand(2.4, 2.5, 100, 0))
        self.assertIsNone(self.policy.departure)


class Sim:
    """Real controller and scheduler driven by offered work on a fake clock."""

    def __init__(self, directory: str, prefill: int = 7) -> None:
        self.now = 0.0
        clock = lambda: self.now
        profiles = ProfileStore(Path(directory) / "profiles.json")
        monitor = InstanceMonitor(clock=clock, profiles=profiles)
        for index in range(8):
            iid = f"e{index}"
            profiles.put(
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
                    decode_fit_mape=0.0,
                    decode_cv_mape=0.0,
                )
            )
            monitor.add(
                Instance(iid, f"http://{iid}", Role.PREFILL if index < prefill else Role.DECODE)
            )
        self.monitor = monitor
        self.events: list[dict[str, object]] = []
        self.scheduler = GlobalScheduler(
            monitor,
            profiles,
            SLO(2.1, 0.045),
            Thresholds(),
            clock=clock,
            decode_concurrency=16,
            on_control_event=self.events.append,
        )
        self.controller = ReactiveController(
            monitor,
            self.scheduler,
            clock=clock,
            window_s=120.0,
            confirmations=2,
            utilization=0.8,
            min_arrivals=10,
            demand_floor=0.5,
        )
        self.telemetry = patch.object(self.scheduler, "pool_load", return_value=0.0)
        self.telemetry.start()
        self.carry = 0.0

    def run(self, seconds: int, workload: tuple[int, int, float]) -> None:
        input_len, output_len, rate = workload
        for _ in range(seconds):
            self.carry += rate
            count = int(self.carry)
            self.carry -= count
            for k in range(count):
                self.controller.saw_arrival(
                    input_len, wanted_len=output_len, at=self.now + (k + 1) / (count + 1)
                )
            self.now += 1.0
            self.controller.sample()
            self.controller.step()

    @property
    def decisions(self) -> list[dict[str, object]]:
        return [event for event in self.events if event["event"] == "controller_decision"]

    def close(self) -> None:
        self.telemetry.stop()


class SustainedShiftTests(unittest.TestCase):
    """A settled fleet moves its first engine within 30 s of a sustained shift."""

    def run_shift(self, burst_s: int | None = None) -> tuple[list, list]:
        with tempfile.TemporaryDirectory() as directory:
            sim = Sim(directory)
            sim.run(300, PREFILL_HEAVY)
            settled = [d for d in sim.decisions if d["result"] == "applied"]
            if burst_s is None:
                sim.run(120, DECODE_HEAVY)
            else:
                sim.run(burst_s, DECODE_HEAVY)
                sim.run(120 - burst_s, PREFILL_HEAVY)
            sim.close()
        moves = [d for d in sim.decisions if d["result"] == "applied" and d["at"] > 300.0]
        return settled, moves

    def test_first_move_follows_a_sustained_shift_within_30_s(self):
        settled, moves = self.run_shift()
        self.assertEqual(settled, [])
        first, second = moves[0], moves[1]
        self.assertLessEqual(first["at"] - 300.0, 30.0)
        self.assertEqual(first["eligibility_rule"], "settled_departure")
        self.assertEqual(first["demand_horizon_s"], first["steady_horizon_s"])
        self.assertEqual(first["demand_horizon_s"], 15.0)
        self.assertEqual(first["required_confirmations"], 1)
        self.assertEqual(second["eligibility_rule"], "source_shrink")
        self.assertEqual(second["demand_horizon_s"], 15.0)
        self.assertGreaterEqual(second["departure_age_s"], 60.0)

    def test_short_burst_moves_no_engine(self):
        _, moves = self.run_shift(burst_s=5)
        self.assertEqual(moves, [])

    def test_burst_shorter_than_one_evidence_span_moves_one_engine(self):
        for burst_s in (20, 30, 45):
            with self.subTest(burst_s=burst_s):
                _, moves = self.run_shift(burst_s=burst_s)
                self.assertEqual([d["eligibility_rule"] for d in moves], ["settled_departure"])


class PostMoveHoldTests(unittest.TestCase):
    """A departure toward decode restarts the evidence hold on decode consolidation."""

    def test_evidence_hold_outlasts_the_departure_hold(self):
        with tempfile.TemporaryDirectory() as directory:
            sim = Sim(directory)
            sim.run(300, PREFILL_HEAVY)
            sim.run(10, DECODE_LEANING)
            self.assertEqual(sim.decisions[-1]["eligibility_rule"], "settled_departure")
            self.assertEqual([flip.to for flip in sim.scheduler.roles.flips], [Role.DECODE])

            sim.run(5, PREFILL_HEAVY)
            row = sim.decisions[-1]
            self.assertEqual((row["prefill"], row["result"]), (7, "held"))
            self.assertEqual(row["reason"], "departure toward decode holds the reverse move")
            self.assertEqual(row["evidence_blocked_gate"], "risk")
            self.assertEqual(row["risk_kind"], "p_to_d_recovery")
            self.assertGreater(row["objective_delta"], sim.controller.movement_margin)
            ControllerDecisionOut.model_validate({k: v for k, v in row.items() if k != "event"})

            # A departure one window old holds no reverse move.
            sim.controller.reactive.departure = Departure(
                -1, sim.now - sim.controller.window_s, moved=True
            )
            sim.run(50, PREFILL_HEAVY)
            row = sim.decisions[-1]
            self.assertIsNone(sim.controller.reactive.departure)
            self.assertEqual(row["evidence_blocked_gate"], "risk")
            self.assertTrue(row["reason"].startswith("p to d recovery"))
            self.assertEqual(len(sim.scheduler.roles.flips), 1)

            sim.run(30, PREFILL_HEAVY)
            sim.close()
        move = [d for d in sim.decisions if d["result"] == "applied"][-1]
        self.assertEqual((move["current_prefill"], move["prefill"]), (6, 7))
        self.assertEqual(move["evidence_blocked_gate"], "none")
        self.assertGreaterEqual(move["risk_age_s"], sim.controller.safety.evidence_span_s)


class AlternationTests(unittest.TestCase):
    """Demand that alternates within one window keeps window pricing."""

    def test_alternation_above_the_calibrated_load_opens_no_departure(self):
        for period, intensity in ((60, 1.1), (60, 1.3), (90, 1.1), (90, 1.3)):
            with self.subTest(period=period, intensity=intensity):
                with tempfile.TemporaryDirectory() as directory:
                    sim = Sim(directory)
                    for _ in range(360 // period):
                        for input_len, output_len, rate in (DECODE_HEAVY, PREFILL_HEAVY):
                            sim.run(period // 2, (input_len, output_len, rate * intensity))
                    sim.close()
                rules = {d.get("eligibility_rule") for d in sim.decisions}
                self.assertFalse(rules & {"settled_departure", "steady_demand"})
                self.assertFalse([d for d in sim.decisions if "departure_age_s" in d])
                self.assertEqual(sim.scheduler.roles.control_snapshot()["flip_reversals"], 0)


class ShortViewTests(unittest.TestCase):
    """The short view prices the current split and its neighbours over the confirmation span."""

    def test_short_view_scores_match_a_full_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            sim = Sim(directory, prefill=4)
            sim.run(60, DECODE_LEANING)
            scorer = sim.controller.scorer
            calls: list[dict[str, object]] = []
            capture = scorer.capture

            def record(*args, **kwargs):
                calls.append(kwargs)
                return capture(*args, **kwargs)

            with patch.object(scorer, "capture", side_effect=record):
                sim.run(5, DECODE_LEANING)
            short_kwargs = next(kwargs for kwargs in calls if "prefills" in kwargs)
            short = capture(sim.now, sim.controller.last_demand, **short_kwargs)
            full_kwargs = {k: v for k, v in short_kwargs.items() if k != "prefills"}
            full = capture(sim.now, sim.controller.last_demand, **full_kwargs)
            sim.close()
        self.assertEqual([p for p, _ in short.demand_options], [3, 4, 5])
        self.assertEqual(len(full.demand_options), 7)
        for p in (3, 4, 5):
            self.assertEqual(short.score(p), full.score(p))

    def test_short_view_spans_the_confirmation_span(self):
        with tempfile.TemporaryDirectory() as directory:
            sim = Sim(directory, prefill=4)
            sim.controller.confirmations_needed = 4
            sim.run(60, DECODE_LEANING)
            scorer = sim.controller.scorer
            spans: list[float] = []
            capture = scorer.capture

            def record(*args, **kwargs):
                if "prefills" in kwargs:
                    spans.append(kwargs["window_s"])
                return capture(*args, **kwargs)

            with patch.object(scorer, "capture", side_effect=record):
                sim.run(10, DECODE_LEANING)
            sim.close()
        self.assertTrue(spans)
        self.assertEqual(set(spans), {sim.controller.step_s * 4})


class ReverseHoldTests(unittest.TestCase):
    """A departure holds its reverse move until the window moves along its heading."""

    def test_window_move_along_the_heading_ends_the_reverse_hold(self):
        with tempfile.TemporaryDirectory() as directory:
            sim = Sim(directory)
            sim.run(300, PREFILL_HEAVY)
            sim.run(15, DECODE_HEAVY)
            policy = sim.controller.reactive
            self.assertEqual([flip.to for flip in sim.scheduler.roles.flips], [Role.DECODE])
            departure = policy.departure
            self.assertEqual((departure.heading, departure.moved), (-1, True))
            # A departure whose shift has ended leaves later moves to the window.
            policy.departure = replace(departure, leading=False)

            sim.run(75, DECODE_HEAVY)
            sim.close()
        moves = [d for d in sim.decisions if d["result"] == "applied"]
        self.assertGreaterEqual(len(moves), 2)
        self.assertEqual(moves[1]["demand_horizon_s"], sim.controller.window_s)
        self.assertEqual({flip.to for flip in sim.scheduler.roles.flips}, {Role.DECODE})
        self.assertLess(sim.now - departure.started_at, sim.controller.window_s)
        self.assertIsNone(policy.departure)

    def test_window_move_before_one_evidence_span_closes_the_departure(self):
        with tempfile.TemporaryDirectory() as directory:
            sim = Sim(directory, prefill=4)
            sim.run(300, DECODE_LEANING)
            sim.run(120, PREFILL_HEAVY)
            sim.close()
        moves = [d for d in sim.decisions if d["result"] == "applied" and d["at"] > 300.0]
        self.assertEqual(moves[0]["eligibility_rule"], "settled_departure")
        self.assertEqual(moves[1]["demand_horizon_s"], sim.controller.window_s)
        self.assertLess(moves[1]["departure_age_s"], 60.0)
        self.assertEqual(moves[2]["demand_horizon_s"], sim.controller.window_s)


class LeadingDepartureTests(unittest.TestCase):
    """After one evidence span, a leading departure moves on demand over the confirmation span."""

    def walk(self, start: int, settle, shift) -> list[dict[str, object]]:
        with tempfile.TemporaryDirectory() as directory:
            sim = Sim(directory, prefill=start)
            sim.run(300, settle)
            sim.run(120, shift)
            sim.close()
        return [d for d in sim.decisions if d["result"] == "applied" and d["at"] > 300.0]

    def test_departure_walks_to_the_far_split_within_115_s(self):
        for start, settle, shift, end in (
            (7, PREFILL_HEAVY, DECODE_HEAVY, 1),
            (1, DECODE_HEAVY, PREFILL_HEAVY, 7),
        ):
            with self.subTest(start=start, end=end):
                moves = self.walk(start, settle, shift)
                self.assertEqual(moves[0]["eligibility_rule"], "settled_departure")
                self.assertEqual(moves[-1]["prefill"], end)
                self.assertLessEqual(moves[-1]["at"] - 300.0, 115.0)
                self.assertEqual(len(moves), abs(end - start))
                self.assertGreaterEqual(moves[1]["departure_age_s"], 60.0)
                for move in moves[1:]:
                    self.assertEqual(move["eligibility_rule"], "source_shrink")
                    self.assertEqual(move["demand_horizon_s"], move["steady_horizon_s"])
                    self.assertLess(move["departure_age_s"], 120.0)


class PendingBodyTests(unittest.TestCase):
    """A request body being read keeps demand complete and the settled run open."""

    def first_moves(self, pending: bool) -> list[dict[str, object]]:
        with tempfile.TemporaryDirectory() as directory:
            sim = Sim(directory)
            sim.run(279, PREFILL_HEAVY)
            demand = sim.controller.demand
            if pending:
                demand.unsized_pending += 1
            sim.run(1, PREFILL_HEAVY)
            if pending:
                demand.resolve_unsized(at=sim.now, retain=False)
            sim.run(20, PREFILL_HEAVY)
            sim.run(120, DECODE_HEAVY)
            sim.close()
        self.assertTrue(all(d["demand_complete"] for d in sim.decisions))
        if pending:
            self.assertEqual(
                next(d for d in sim.decisions if d["at"] == 280.0)["unsized_offers"], 1
            )
        return [d for d in sim.decisions if d["result"] == "applied"]

    def test_pending_body_leaves_settled_run_and_departure_unchanged(self):
        moves = self.first_moves(pending=True)
        self.assertTrue(all(d["at"] > 300.0 for d in moves))
        first = moves[0]
        self.assertLessEqual(first["at"] - 300.0, 30.0)
        self.assertEqual(first["eligibility_rule"], "settled_departure")
        self.assertEqual(first["at"], self.first_moves(pending=False)[0]["at"])


def bound_prefill(sim: Sim, max_tokens: int = 16300) -> None:
    """End every engine's measured prefill sweep at `max_tokens`."""
    for iid in list(sim.monitor.instances):
        row = sim.scheduler.profiles.get(iid)
        sim.scheduler.profiles.put(
            replace(row, prefill_min_tokens=256, prefill_max_tokens=max_tokens)
        )


class BeyondProfileShiftTests(unittest.TestCase):
    """Role control keeps moving engines with prompts beyond the prefill sweep or unsized offers."""

    def shift(self, documents: tuple[int, int, float], *, unsized: int = 0) -> list[dict]:
        with tempfile.TemporaryDirectory() as directory:
            sim = Sim(directory, prefill=1)
            bound_prefill(sim)
            sim.run(270, DECODE_HEAVY)
            demand = sim.controller.demand
            for k in range(unsized):
                demand.unsized_pending += 1
                demand.resolve_unsized(at=sim.now - 1.0 + k * 0.3, retain=True)
            sim.run(30, DECODE_HEAVY)
            sim.run(60, documents)
            sim.close()
        return [
            d
            for d in sim.decisions
            if d["at"] > 300.0 and d["result"] == "applied" and d["prefill"] > d["current_prefill"]
        ]

    def test_prompts_above_the_sweep_move_an_engine_to_prefill_within_30_s(self):
        first = self.shift((18_000, 150, 2.0))[0]
        self.assertLessEqual(first["at"] - 300.0, 30.0)
        self.assertEqual(first["eligibility_rule"], "settled_departure")
        self.assertTrue(first["demand_complete"])
        self.assertGreater(first["arrivals_beyond_profile"], 0)
        ControllerDecisionOut.model_validate({k: v for k, v in first.items() if k != "event"})

    def test_unsized_offers_leave_the_shift_priced(self):
        first = self.shift((12_000, 150, 2.0), unsized=3)[0]
        self.assertLessEqual(first["at"] - 300.0, 30.0)
        self.assertEqual(first["eligibility_rule"], "settled_departure")
        self.assertTrue(first["demand_complete"])
        self.assertEqual(first["unsized_offers"], 3)
        ControllerDecisionOut.model_validate({k: v for k, v in first.items() if k != "event"})


class PhaseTraceTests(unittest.TestCase):
    """Each long-document phase walks to prefill after a generation phase with unsized offers."""

    DOC = (6.0, (16_000, 20_000), (100, 200))
    GEN = (7.1, (200, 400), (2_000, 3_000))
    CHAT = (0.5, (300, 800), (500, 1_500))
    PHASE_S = 180

    def test_document_phases_walk_to_prefill_with_complete_demand(self):
        rng = random.Random(7)
        carry: dict[str, float] = {}
        with tempfile.TemporaryDirectory() as directory:
            sim = Sim(directory, prefill=1)
            bound_prefill(sim)
            sim.scheduler.decode_concurrency = 48
            for iid in list(sim.monitor.instances):
                row = sim.scheduler.profiles.get(iid)
                sim.scheduler.profiles.put(
                    replace(row, tpot_request_slope=0.00015, decode_max_requests=48)
                )
            demand = sim.controller.demand
            splits = []
            for phase in ("gen", "doc", "gen", "doc"):
                for _ in range(self.PHASE_S):
                    rows = []
                    for name, (rate, inputs, outputs) in (
                        (phase, self.DOC if phase == "doc" else self.GEN),
                        ("chat", self.CHAT),
                    ):
                        carry[name] = carry.get(name, 0.0) + rate
                        count = int(carry[name])
                        carry[name] -= count
                        rows += [
                            (
                                sim.now + (k + 1) / (count + 1),
                                rng.randint(*inputs),
                                rng.randint(*outputs),
                            )
                            for k in range(count)
                        ]
                    for at, input_len, output_len in sorted(rows):
                        if phase == "gen" and rng.random() < 0.1:
                            demand.unsized_pending += 1
                            demand.resolve_unsized(at=at, retain=True)
                        else:
                            sim.controller.saw_arrival(input_len, wanted_len=output_len, at=at)
                    sim.now += 1.0
                    sim.controller.sample()
                    sim.controller.step()
                    splits.append(len(sim.monitor.pool(Role.PREFILL)))
            sim.close()
        decisions = sim.decisions
        self.assertTrue(all(d["demand_complete"] for d in decisions))
        self.assertTrue(any(d.get("unsized_offers") for d in decisions))
        for start in (180, 540):
            with self.subTest(phase_start=start):
                toward = [
                    d
                    for d in decisions
                    if start < d["at"] <= start + self.PHASE_S
                    and d["result"] == "applied"
                    and d["prefill"] > d["current_prefill"]
                ]
                self.assertLessEqual(toward[0]["at"] - start, 30.0)
                self.assertEqual(toward[0]["eligibility_rule"], "settled_departure")
                self.assertGreater(toward[0]["arrivals_beyond_profile"], 0)
                self.assertEqual(toward[1]["demand_horizon_s"], toward[1]["steady_horizon_s"])
                self.assertGreaterEqual(toward[1]["departure_age_s"], 60.0)
                reached = next(t for t in range(start, start + self.PHASE_S) if splits[t] >= 6)
                self.assertLessEqual(reached + 1 - start, 95)


if __name__ == "__main__":
    unittest.main()
