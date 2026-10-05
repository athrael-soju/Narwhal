"""Check the bookkeeping micro-benchmark's measured items and output."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tools.measurement import bookkeeping_bench as bench


class BookkeepingBenchTests(unittest.TestCase):
    def test_every_scoped_item_reports_both_sizes(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = bench.measure(Path(directory), calls=50, rounds=1)
        items = [(row["item"], row["size"]) for row in rows]
        self.assertEqual(
            items,
            [
                ("output_token", "10 resident decode requests on the engine"),
                ("output_token", "50 resident decode requests on the engine"),
                ("decode_admits", "32 engines, 30 resident decode requests"),
                ("decode_admits", "32 engines, 800 resident decode requests"),
                ("project_prefill", "5 waiting requests"),
                ("project_prefill", "50 waiting requests"),
                ("schedule (prefill)", "8 engines"),
                ("schedule (prefill)", "32 engines"),
            ],
        )
        self.assertTrue(all(row["per_call_us"] > 0 for row in rows))

    def test_main_prints_one_json_row_per_measurement(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(bench.main(["--calls", "50", "--rounds", "1"]), 0)
        rows = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(len(rows), 8)


if __name__ == "__main__":
    unittest.main()
