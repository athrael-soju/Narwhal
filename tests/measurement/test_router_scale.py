"""Check the multi-router scale run's allocation, sampling summaries and report."""

import io
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from tools.measurement.router_benchmark import cli as bench_cli
from tools.measurement.router_benchmark import scale


def sample(pools, resident, engines):
    return {
        "routers": [{"pools": p, "resident": r} for p, r in zip(pools, resident, strict=True)],
        "engines": engines,
    }


POOLS = {"prefill": ["e0"], "decode": ["e1", "e2"]}
FLIPPED = {"prefill": ["e0", "e1"], "decode": ["e2"]}


def resident(e1, e2):
    return {
        "e0": {"prefill": 0, "decode": 0},
        "e1": {"prefill": 0, "decode": e1},
        "e2": {"prefill": 0, "decode": e2},
    }


def active(e1, e2):
    return {
        "e0": {"prefill": 0.0, "decode": 0.0},
        "e1": {"prefill": 0.0, "decode": e1},
        "e2": {"prefill": 0.0, "decode": e2},
    }


class AllocationTests(unittest.TestCase):
    def test_routers_take_the_first_cpus_and_their_siblings_stay_idle(self):
        allocation = scale.allocate_scale(
            list(range(20)), {0: [0, 10], 1: [1, 11]}, routers=2, clients=4, engines=6
        )
        self.assertEqual(allocation.routers, [0, 1])
        self.assertEqual(allocation.router_siblings, [10, 11])
        self.assertEqual(allocation.clients, [2, 3, 4, 5])
        self.assertEqual(allocation.engines, [6, 7, 8, 9, 12, 13])
        self.assertEqual(allocation.driver, 19)

    def test_siblings_outside_the_cpu_list_take_no_cpu(self):
        allocation = scale.allocate_scale(
            list(range(8)), {0: [0, 40], 1: [1, 41]}, routers=2, clients=2, engines=3
        )
        self.assertEqual(allocation.router_siblings, [40, 41])
        self.assertEqual(allocation.clients, [2, 3])
        self.assertEqual(allocation.engines, [4, 5, 6])
        self.assertEqual(allocation.driver, 7)

    def test_too_few_cpus_names_every_need(self):
        with self.assertRaisesRegex(ValueError, "needs 12: 2 routers, 0 idle router siblings"):
            scale.allocate_scale(list(range(8)), {}, routers=2, clients=4, engines=5)


class SummaryTests(unittest.TestCase):
    def test_engine_gauges_parse_by_phase(self):
        text = (
            'simulated_engine_active_streams{phase="prefill"} 2\n'
            'simulated_engine_active_streams{phase="decode"} 31\n'
            'simulated_engine_peak_streams{phase="decode"} 40\n'
        )
        self.assertEqual(scale.engine_active(text), {"prefill": 2.0, "decode": 31.0})

    def test_role_disagreement_counts_samples_with_differing_pools(self):
        samples = [
            sample([POOLS, POOLS], [resident(1, 1)] * 2, active(2, 2)),
            sample([POOLS, FLIPPED], [resident(1, 1)] * 2, active(2, 2)),
            sample([FLIPPED, FLIPPED], [resident(1, 1)] * 2, active(2, 2)),
        ]
        self.assertEqual(scale.disagreement_seconds(samples), 1)

    def test_resident_gap_sums_routers_against_engine_streams(self):
        samples = [
            sample([POOLS, POOLS], [resident(3, 1), resident(2, 4)], active(5, 4)),
            sample([POOLS, POOLS], [resident(0, 0), resident(1, 1)], active(1, 2)),
        ]
        self.assertEqual(
            scale.resident_gap(samples),
            {"mean_router_decode": 6.0, "mean_engine_decode": 6.0, "mean_abs_gap": 1.0},
        )

    def test_scale_row_and_point(self):
        rows = [
            {
                "outcome": "completed",
                "token_events": 10,
                "scheduled_mono": 0.0,
                "schedule_lag_s": 0.0,
                "elapsed_s": 2.0,
            },
            {
                "outcome": "completed",
                "token_events": 10,
                "scheduled_mono": 1.0,
                "schedule_lag_s": 0.0,
                "elapsed_s": 3.0,
            },
        ]
        edge = {
            "cpu_s": 1.0,
            "metrics": {"narwhal_event_loop_busy_seconds_total": 1.0},
            "rejected": 4,
            "refused": 1,
        }
        later = {
            "cpu_s": 3.0,
            "metrics": {"narwhal_event_loop_busy_seconds_total": 2.0},
            "rejected": 4,
            "refused": 1,
        }
        drained = later | {"refused": 2}
        samples = [sample([POOLS, POOLS], [resident(1, 0), resident(0, 1)], active(1, 1))]
        row = scale.scale_row(
            5.0,
            2,
            rows,
            samples,
            {
                "start": [edge, edge],
                "before": [edge, edge],
                "after": [later, later],
                "drained": [drained, later],
            },
            4.0,
        )
        self.assertEqual(row["relayed_frames"], 20)
        self.assertEqual(row["relayed_frames_per_s"], 5.0)
        self.assertEqual(row["requests_per_s"], 0.5)
        self.assertEqual([r["cpu_s"] for r in row["routers"]], [2.0, 2.0])
        self.assertEqual([r["busy_share"] for r in row["routers"]], [0.25, 0.25])
        self.assertEqual((row["saturation_rejections"], row["refused"]), (0, 1))
        self.assertEqual(row["role_disagreement_s"], 0)
        self.assertEqual(row["max_engine_decode_streams"], 1.0)
        saturated = row | {"offered_rps": 10.0, "saturation_rejections": 3}
        point = scale.select_scale_point([row, saturated])
        self.assertEqual(point["offered_rps"], 5.0)
        doc = {
            "label": "two",
            "routers": 2,
            "router_source": "sha",
            "rates": [row, saturated],
            "point": point,
            "stopped_by": "saturation",
        }
        text = scale.scale_text(doc)
        self.assertIn("5 5.0 0.50 0 1 0.25,0.25 0", text)
        self.assertIn("point: 5 rps, 5.0 frames/s, 0.50 requests/s", text)


class OptionTests(unittest.TestCase):
    def test_scale_requires_at_least_one_router(self):
        parser = bench_cli.parser_for()
        args = parser.parse_args(
            [
                "scale",
                "--router-src",
                "src",
                "--routers",
                "0",
                "--cpus",
                "0-40",
                "--rates",
                "5",
                "--out",
                "/nonexistent/scale",
            ]
        )
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            bench_cli.check(parser, args)
        args.routers = 2
        args.out = Path("/nonexistent/scale")
        bench_cli.check(parser, args)


if __name__ == "__main__":
    unittest.main()
