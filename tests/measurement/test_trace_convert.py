"""Convert public trace formats to time-scaled `mooncake_trace` files."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tools.measurement import trace_convert


class ConverterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def source(self, name, text):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def records(self, kind, text, **options):
        path = self.source(f"source.{kind}", text)
        return trace_convert.convert(trace_convert.read_arrivals(path, kind), **options)

    def test_azure_timestamps_become_milliseconds_from_the_first_request(self):
        for stamps in (
            ("2023-11-16 18:17:03.9799600", "2023-11-16 18:17:04.4799600"),
            ("2024-05-10 00:00:00.009930+00:00", "2024-05-10 00:00:00.509930+00:00"),
        ):
            with self.subTest(first=stamps[0]):
                records, result = self.records(
                    "azure",
                    "TIMESTAMP,ContextTokens,GeneratedTokens\n"
                    f"{stamps[0]},4808,10\n{stamps[1]},3180,8\n",
                )
                self.assertEqual(
                    records,
                    [
                        {"timestamp": 0.0, "input_length": 4808, "output_length": 10},
                        {"timestamp": 500.0, "input_length": 3180, "output_length": 8},
                    ],
                )
                self.assertEqual(result.offered_rps(), 2.0)

    def test_time_scale_divides_every_gap_and_keeps_the_arrival_shape(self):
        text = "Timestamp,Model,Request tokens,Response tokens,Total tokens,Log Type\n"
        text += "".join(f"{t},ChatGPT,100,10,110,Conversation log\n" for t in (5, 6, 9, 17))
        plain, plain_result = self.records("burstgpt", text)
        scaled, scaled_result = self.records("burstgpt", text, time_scale=4)
        self.assertEqual([r["timestamp"] for r in plain], [0, 1000, 4000, 12000])
        self.assertEqual([r["timestamp"] for r in scaled], [0, 250, 1000, 3000])
        self.assertEqual(scaled_result.offered_rps(), 4 * plain_result.offered_rps())
        self.assertEqual(scaled_result.source_span_s, 12)
        self.assertEqual(scaled_result.replay_span_s, 3)

    def test_burstgpt_rows_without_tokens_are_skipped(self):
        records, result = self.records(
            "burstgpt",
            "Timestamp,Model,Request tokens,Response tokens,Total tokens,Log Type\n"
            "5,ChatGPT,472,18,490,Conversation log\n"
            "45,ChatGPT,1087,0,1087,Conversation log\n"
            "118,GPT-4,0,276,276,API log\n"
            "185,ChatGPT,1360,647,2007,Conversation log\n",
        )
        self.assertEqual([r["input_length"] for r in records], [472, 1360])
        self.assertEqual(result.skipped, 2)

    def test_requests_over_the_token_cap_are_skipped(self):
        text = "".join(
            json.dumps({"timestamp": t, "input_length": n, "output_length": 10}) + "\n"
            for t, n in ((0, 90), (1000, 91), (2000, 50))
        )
        records, result = self.records("mooncake", text, max_total_tokens=100)
        self.assertEqual([r["input_length"] for r in records], [90, 50])
        self.assertEqual(result.skipped, 1)

    def test_clip_keeps_a_window_after_the_first_request_and_rebases_it(self):
        text = "".join(
            json.dumps({"timestamp": t, "input_length": 10, "output_length": 2, "hash_ids": [t]})
            + "\n"
            for t in (0, 3000, 6000, 9000, 12000)
        )
        records, result = self.records("mooncake", text, start_s=3, duration_s=6)
        self.assertEqual(
            records,
            [
                {"timestamp": 0.0, "input_length": 10, "output_length": 2, "hash_ids": [3000]},
                {"timestamp": 3000.0, "input_length": 10, "output_length": 2, "hash_ids": [6000]},
            ],
        )
        self.assertEqual(result.requests, 2)

    def test_unreadable_sources_name_the_line(self):
        cases = (
            ("azure", "TIMESTAMP,ContextTokens,GeneratedTokens\n1-16 18:26:33.72,932,57\n", ":2:"),
            ("azure", "TIMESTAMP,ContextTokens\n2023-11-16 18:17:03,1\n", "GeneratedTokens"),
            ("burstgpt", "Timestamp,Request tokens,Response tokens\n5,ten,3\n", ":2:"),
            ("mooncake", '{"timestamp": 0, "input_length": 1, "output_length": 1}\n{}\n', ":2:"),
        )
        for kind, text, expected in cases:
            with (
                self.subTest(kind=kind, expected=expected),
                self.assertRaisesRegex(ValueError, expected),
            ):
                self.records(kind, text)

    def test_an_empty_clip_and_invalid_options_are_refused(self):
        text = '{"timestamp": 0, "input_length": 1, "output_length": 1}\n'
        for options in ({"start_s": 1}, {"time_scale": 0}, {"duration_s": 0}, {"start_s": -1}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.records("mooncake", text, **options)

    def test_cli_writes_a_new_owner_only_file_and_reports_the_rate(self):
        source = self.source(
            "azure.csv",
            "TIMESTAMP,ContextTokens,GeneratedTokens\n"
            "2023-11-16 18:00:00.0,100,10\n2023-11-16 18:00:01.0,200,20\n"
            "2023-11-16 18:00:02.0,300,30\n",
        )
        out = self.root / "trace.jsonl"
        argv = ["--format", "azure", "--input", str(source), "--out", str(out)]
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(trace_convert.main([*argv, "--time-scale", "2"]), 0)
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {
                "requests": 3,
                "skipped": 0,
                "source_span_s": 2.0,
                "replay_span_s": 1.0,
                "offered_rps": 2.0,
            },
        )
        lines = [json.loads(line) for line in out.read_text().splitlines()]
        self.assertEqual([line["timestamp"] for line in lines], [0, 500, 1000])
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(trace_convert.main(argv), 1)


if __name__ == "__main__":
    unittest.main()
