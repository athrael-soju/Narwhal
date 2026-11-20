"""Score AIPerf records of a trace replay against TTFT and TPOT SLOs."""

import json
import tempfile
import unittest
from pathlib import Path

from tools.measurement import attainment


def record(
    ttft_ms=100.0, itl_ms=20.0, tokens=10, *, error=None, cancelled=False, phase="profiling"
):
    metrics = {
        "time_to_first_token": {"value": ttft_ms, "unit": "ms"},
        "output_sequence_length": {"value": tokens, "unit": "tokens"},
    }
    if itl_ms is not None:
        metrics["inter_token_latency"] = {"value": itl_ms, "unit": "ms"}
    return {
        "metadata": {"benchmark_phase": phase, "was_cancelled": cancelled},
        "metrics": metrics,
        "error": error,
    }


class WithinSloTests(unittest.TestCase):
    def test_a_request_passes_only_within_both_limits(self):
        cases = (
            (record(), True),
            (record(ttft_ms=500), False),
            (record(itl_ms=60), False),
            (record(itl_ms=None, tokens=1), True),
            (record(itl_ms=None, tokens=5), False),
            (record(itl_ms=None, tokens=0), False),
            (record(error={"code": 503, "type": "http"}), False),
            (record(cancelled=True), False),
        )
        for row, expected in cases:
            with self.subTest(row=row):
                self.assertIs(attainment.within_slo(row, 0.2, 0.05), expected)

    def test_units_convert_to_seconds(self):
        row = record()
        row["metrics"]["time_to_first_token"] = {"value": 0.3, "unit": "s"}
        self.assertFalse(attainment.within_slo(row, 0.2, 0.05))
        row["metrics"]["time_to_first_token"] = {"value": 0.3, "unit": "minutes"}
        with self.assertRaisesRegex(ValueError, "unit"):
            attainment.within_slo(row, 0.2, 0.05)


class ScoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, name, rows):
        path = self.root / name
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        return path

    def test_every_offered_request_counts_and_warmup_is_left_out(self):
        path = self.write(
            "records.jsonl", [record(phase="warmup"), record(), record(), record(ttft_ms=900)]
        )
        self.assertEqual(
            attainment.score(path, 4, 0.2, 0.05, 0.5),
            {"offered": 4, "recorded": 3, "within_slo": 2, "attainment": 0.5, "met": True},
        )
        self.assertFalse(attainment.score(path, 4, 0.2, 0.05, 0.9)["met"])

    def test_more_records_than_offered_requests_are_refused(self):
        path = self.write("records.jsonl", [record(), record()])
        with self.assertRaisesRegex(ValueError, "2 records for a trace of 1"):
            attainment.score(path, 1, 0.2, 0.05, 0.9)

    def test_an_unreadable_record_names_the_line(self):
        path = self.root / "records.jsonl"
        path.write_text(json.dumps(record()) + "\n{not json\n")
        with self.assertRaisesRegex(ValueError, ":2:"):
            attainment.score(path, 2, 0.2, 0.05, 0.9)

    def test_malformed_records_and_records_without_ttft_are_refused(self):
        cases = (
            ({"metadata": {}}, "not an AIPerf record"),
            ({"metadata": {}, "metrics": {}, "error": "boom"}, "not an AIPerf record"),
        )
        for row, expected in cases:
            with self.subTest(row=row):
                path = self.write("records.jsonl", [row])
                with self.assertRaisesRegex(ValueError, expected):
                    attainment.score(path, 1, 0.2, 0.05, 0.9)
        unstreamed = record()
        del unstreamed["metrics"]["time_to_first_token"]
        path = self.write("records.jsonl", [unstreamed, unstreamed])
        with self.assertRaisesRegex(ValueError, "--streaming"):
            attainment.score(path, 2, 0.2, 0.05, 0.9)

    def test_failed_and_cancelled_records_count_as_misses(self):
        path = self.write(
            "records.jsonl",
            [record(), record(error={"code": 500, "type": "http"}), record(cancelled=True)],
        )
        self.assertEqual(attainment.score(path, 4, 0.2, 0.05, 0.9)["within_slo"], 1)

    def test_trace_rate_reads_the_replay_file(self):
        path = self.write(
            "trace.jsonl",
            [{"timestamp": t, "input_length": 5, "output_length": 5} for t in (0, 500, 1000)],
        )
        rate = attainment.trace_rate(path)
        self.assertEqual(len(rate.pop("sha256")), 64)
        self.assertEqual(rate, {"requests": 3, "replay_span_s": 1.0, "offered_rps": 2.0})

    def test_trace_rate_needs_two_arrival_times(self):
        for stamps in ((0,), (5, 5)):
            with self.subTest(stamps=stamps):
                path = self.write(
                    "trace.jsonl",
                    [{"timestamp": t, "input_length": 5, "output_length": 5} for t in stamps],
                )
                with self.assertRaisesRegex(ValueError, "two requests"):
                    attainment.trace_rate(path)


class HighestRateTests(unittest.TestCase):
    def point(self, name, trace, rps, met):
        return {
            "id": name,
            "trace": trace,
            "attainment": {"offered_rps": rps, "attainment": 0.95 if met else 0.5, "met": met},
        }

    def test_each_trace_reports_its_highest_rate_that_met_the_share(self):
        report = attainment.highest_rates(
            [
                self.point("code-x4", "code", 4.0, False),
                self.point("code-x1", "code", 1.0, True),
                self.point("code-x2", "code", 2.0, True),
                self.point("burst-x1", "burst", 0.5, False),
            ]
        )
        self.assertEqual(report["code"]["highest_rps"], 2.0)
        self.assertEqual(report["code"]["highest_point"], "code-x2")
        self.assertEqual(
            [p["id"] for p in report["code"]["points"]], ["code-x1", "code-x2", "code-x4"]
        )
        self.assertIsNone(report["burst"]["highest_rps"])
        self.assertTrue(report["code"]["monotone"])

    def test_a_pass_above_the_first_miss_is_not_the_highest_rate(self):
        report = attainment.highest_rates(
            [
                self.point("x1", "code", 1.0, True),
                self.point("x2", "code", 2.0, False),
                self.point("x3", "code", 3.0, True),
            ]
        )["code"]
        self.assertEqual(report["highest_point"], "x1")
        self.assertFalse(report["monotone"])


if __name__ == "__main__":
    unittest.main()
