"""Live load-job results read incrementally from AIPerf's per-request records."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tools.fleet_control.live import LiveRecords, percentile

SECOND = 1_000_000_000


def record(
    start_s: float,
    end_s: float,
    *,
    ttft: float = 100.0,
    itl: float = 10.0,
    tokens: int = 50,
    phase: str = "profiling",
    error: dict[str, Any] | None = None,
    good: int | None = None,
) -> str:
    metrics: dict[str, Any] = {
        "time_to_first_token": {"value": ttft, "unit": "ms"},
        "inter_token_latency": {"value": itl, "unit": "ms"},
        "request_latency": {"value": (end_s - start_s) * 1000, "unit": "ms"},
        "output_token_count": {"value": tokens, "unit": "tokens"},
    }
    if good is not None:
        metrics["good_request_count"] = {"value": good, "unit": "count"}
    row = {
        "metadata": {
            "benchmark_phase": phase,
            "request_start_ns": int(start_s * SECOND),
            "request_end_ns": int(end_s * SECOND),
        },
        "metrics": {} if error else metrics,
        "error": error,
    }
    return json.dumps(row) + "\n"


class LiveRecordsTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "profile_export.jsonl"
        self.records = LiveRecords(self.path)

    def append(self, text: str) -> None:
        with self.path.open("a") as stream:
            stream.write(text)

    def test_a_missing_file_reports_no_requests(self) -> None:
        self.records.refresh()
        result = self.records.results({})
        self.assertEqual(result["requests"]["total"], 0)
        self.assertEqual(result["series"]["points"], [])

    def test_results_count_rates_and_latency_of_profiling_requests(self) -> None:
        self.append(record(0, 1, phase="warmup"))
        self.append(record(10, 11, ttft=100, good=1))
        self.append(record(11, 13, ttft=300, good=0))
        self.append(record(12, 14, error={"code": 503, "type": "HTTPError"}))
        self.records.refresh()
        result = self.records.results({"time_to_first_token": 250.0})
        requests = result["requests"]
        self.assertEqual(
            (requests["total"], requests["completed"], requests["errors"], requests["warmup"]),
            (3, 2, 1, 1),
        )
        self.assertEqual(requests["errors_by_type"], {"http_503": 1})
        self.assertEqual(result["elapsed_s"], 4.0)
        self.assertEqual(result["throughput"]["requests_per_s"], 0.5)
        self.assertEqual(result["throughput"]["output_tokens_per_s"], 25.0)
        self.assertEqual(result["latency"]["ttft"]["p50"], 200.0)
        self.assertEqual(result["slo"], {"ttft": 250.0})
        self.assertEqual(result["goodput"], {"good_requests": 1, "requests_per_s": 0.25})
        (point,) = result["series"]["points"]
        self.assertEqual((point["t"], point["errors"]), (5, 1))
        self.assertEqual(point["requests_per_s"], 0.4)

    def test_refresh_reads_only_complete_new_lines(self) -> None:
        line = record(0, 1)
        self.append(record(0, 1) + line[:20])
        self.records.refresh()
        self.assertEqual(len(self.records.samples), 1)
        self.append(line[20:])
        self.records.refresh()
        self.assertEqual(len(self.records.samples), 2)
        self.records.refresh()
        self.assertEqual(len(self.records.samples), 2)

    def test_a_replaced_file_is_read_from_the_start(self) -> None:
        self.append(record(0, 1) + record(1, 2))
        self.records.refresh()
        self.path.unlink()
        self.append(record(0, 1))
        self.records.refresh()
        self.assertEqual(len(self.records.samples), 1)

    def test_unreadable_lines_are_counted(self) -> None:
        self.append("not json\n" + record(0, 1))
        self.records.refresh()
        result = self.records.results({})
        self.assertEqual(result["requests"]["unreadable"], 1)
        self.assertEqual(result["requests"]["completed"], 1)

    def test_a_long_job_keeps_at_most_120_points(self) -> None:
        self.append("".join(record(t, t + 1) for t in range(0, 3600, 10)))
        self.records.refresh()
        series = self.records.results({})["series"]
        self.assertLessEqual(len(series["points"]), 120)
        self.assertEqual(series["bucket_s"] % 5, 0)

    def test_percentile_interpolates_between_ranks(self) -> None:
        self.assertEqual(percentile([1.0, 2.0, 3.0, 4.0], 0.5), 2.5)
        self.assertEqual(percentile([7.0], 0.99), 7.0)


if __name__ == "__main__":
    unittest.main()
