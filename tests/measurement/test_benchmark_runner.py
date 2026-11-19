"""Exercise the real benchmark runner and a separate client process over HTTP."""

import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tools.measurement import benchmark_runner as runner


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.busy_polls = 0
        self.ready = True
        self.model = "test-model"
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                status = 200
                if self.path == "/ready":
                    status = 200 if outer.ready else 503
                    body = {"status": "ready" if outer.ready else "not_ready"}
                elif self.path == "/v1/models":
                    body = {"data": [{"id": outer.model}]}
                elif self.path == "/narwhal/state":
                    outer.events.append("state")
                    body = {
                        "admission": {
                            "inflight": int(outer.busy_polls > 0),
                            "queued": 0,
                            "waiting_prefill": 0,
                            "waiting_decode": 0,
                        },
                        "serving": {"http_retained": 0},
                        "resident": {"e1": {"prefill": 0, "decode": 0}},
                    }
                    outer.busy_polls = max(0, outer.busy_polls - 1)
                elif self.path.startswith("/start/"):
                    outer.events.append(self.path)
                    outer.busy_polls = 2
                    body = {"ok": True}
                else:
                    status, body = 404, {"error": "unknown path"}
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.worker.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def plan(self, *, exit_code=0, drain_timeout=3):
        code = (
            "import sys, urllib.request; "
            "urllib.request.urlopen(sys.argv[1] + '/start/' + sys.argv[3]).read(); "
            "sys.exit(int(sys.argv[4]))"
        )
        return {
            "schema": 1,
            "points": [
                {
                    "id": name,
                    "workload": {"rate_rps": rate},
                    "client_argv": [
                        sys.executable,
                        "-c",
                        code,
                        "{base}",
                        "{model}",
                        "{point_id}",
                        str(exit_code),
                    ],
                    "client_timeout_s": 5,
                    "drain_timeout_s": drain_timeout,
                }
                for name, rate in (("slow", 0.5), ("fast", 1.0))
            ],
        }

    def execute(self, plan):
        root = Path(self.temp.name)
        path = root / "plan.json"
        path.write_text(json.dumps(plan))
        out = root / "run"
        status = runner.main(
            ["--base", self.base, "--model", "test-model", "--plan", str(path), "--out", str(out)]
        )
        return status, out

    def test_two_points_wait_for_drain_and_retain_inputs(self):
        status, out = self.execute(self.plan())
        self.assertEqual(status, 0)
        self.assertLess(self.events.index("/start/slow"), self.events.index("/start/fast"))
        between = self.events[
            self.events.index("/start/slow") + 1 : self.events.index("/start/fast")
        ]
        self.assertGreaterEqual(between.count("state"), 3)
        self.assertEqual(
            json.loads((out / "slow/result.json").read_text())["drain"]["condition"], "idle"
        )
        self.assertEqual(
            json.loads((out / "fast/result.json").read_text())["condition"], "completed"
        )
        self.assertEqual(
            json.loads((out / "manifest.json").read_text())["plan"]["points"][0]["workload"],
            {"rate_rps": 0.5},
        )
        self.assertEqual(out.stat().st_mode & 0o777, 0o700)
        self.assertEqual((out / "slow/result.json").stat().st_mode & 0o777, 0o600)

    def test_readiness_refusal_and_model_mismatch_stop_before_client(self):
        for setting, value, expected in (
            ("ready", False, "readiness_refused"),
            ("model", "other", "model_mismatch"),
        ):
            with self.subTest(setting=setting):
                setattr(self, setting, value)
                status, out = self.execute(self.plan())
                self.assertEqual(status, 1)
                self.assertEqual(
                    json.loads((out / "slow/result.json").read_text())["condition"], expected
                )
                self.assertFalse((out / "fast").exists())
                self.assertFalse(any(event.startswith("/start/") for event in self.events))
                self.events.clear()
                setattr(self, setting, True if setting == "ready" else "test-model")
                Path(self.temp.name, "run").rename(Path(self.temp.name, f"previous-{setting}"))

    def test_client_failure_still_drains_and_stops(self):
        status, out = self.execute(self.plan(exit_code=7))
        self.assertEqual(status, 1)
        record = json.loads((out / "slow/result.json").read_text())
        self.assertEqual(record["condition"], "client_failure")
        self.assertEqual(record["client"]["exit_status"], 7)
        self.assertEqual(record["drain"]["condition"], "idle")
        self.assertFalse((out / "fast").exists())

    def traced_plan(self, ttfts):
        """Return a plan whose clients write one AIPerf record per TTFT, in milliseconds."""
        code = (
            "import json, pathlib, sys, urllib.request; "
            "urllib.request.urlopen(sys.argv[1] + '/start/' + sys.argv[3]).read(); "
            "out = pathlib.Path(sys.argv[4]); out.mkdir(); "
            "rows = [{'metadata': {'benchmark_phase': 'profiling'}, 'error': None, "
            "'metrics': {'time_to_first_token': {'value': t, 'unit': 'ms'}, "
            "'inter_token_latency': {'value': 10, 'unit': 'ms'}}} "
            "for t in json.loads(sys.argv[5])]; "
            "(out / 'profile_export.jsonl').write_text("
            "''.join(json.dumps(r) + '\\n' for r in rows))"
        )
        root = Path(self.temp.name)
        plan = self.plan()
        for point, scale in zip(plan["points"], (1, 2), strict=True):
            trace = root / f"trace-x{scale}.jsonl"
            trace.write_text(
                "".join(
                    json.dumps({"timestamp": t / scale, "input_length": 4, "output_length": 4})
                    + "\n"
                    for t in (0, 1000, 2000, 3000)
                )
            )
            point["trace"] = {"name": "code", "file": str(trace)}
            point["client_argv"] = [
                sys.executable,
                "-c",
                code,
                "{base}",
                "{model}",
                "{point_id}",
                "{point_dir}/aiperf",
                json.dumps(ttfts[point["id"]]),
            ]
        plan["attainment"] = {"ttft_s": 0.2, "tpot_s": 0.05, "share": 0.75}
        return plan

    def test_traced_points_report_the_highest_rate_within_the_slo_share(self):
        status, out = self.execute(
            self.traced_plan({"slow": [100, 100, 100, 900], "fast": [100, 900, 900, 900]})
        )
        self.assertEqual(status, 0)
        slow = json.loads((out / "slow/result.json").read_text())["attainment"]
        self.assertEqual(slow["offered_rps"], 1.0)
        self.assertEqual(slow["attainment"], 0.75)
        report = json.loads((out / "attainment.json").read_text())
        self.assertEqual(report["share"], 0.75)
        self.assertEqual(report["traces"]["code"]["highest_rps"], 1.0)
        self.assertEqual(report["traces"]["code"]["highest_point"], "slow")
        self.assertEqual([p["offered_rps"] for p in report["traces"]["code"]["points"]], [1.0, 2.0])

    def test_missing_client_records_stop_with_an_attainment_error(self):
        plan = self.traced_plan({"slow": [100], "fast": [100]})
        plan["attainment"]["client_records"] = "{point_dir}/missing.jsonl"
        status, out = self.execute(plan)
        self.assertEqual(status, 1)
        record = json.loads((out / "slow/result.json").read_text())
        self.assertEqual(record["condition"], "attainment_error")
        self.assertFalse((out / "fast").exists())
        self.assertEqual(json.loads((out / "attainment.json").read_text())["traces"], {})

    def test_trace_plans_are_validated_before_the_run(self):
        cases = (
            ("attainment", None, "require an attainment object"),
            ("share", 1.5, "attainment.share"),
            ("client_records", "records.jsonl", "{point_dir}"),
            ("file", "missing.jsonl", "slow: trace file"),
        )
        for key, value, expected in cases:
            with self.subTest(key=key):
                plan = self.traced_plan({"slow": [], "fast": []})
                if key == "attainment":
                    del plan["attainment"]
                elif key == "file":
                    plan["points"][0]["trace"]["file"] = str(Path(self.temp.name, value))
                else:
                    plan["attainment"][key] = value
                path = Path(self.temp.name, "plan.json")
                path.write_text(json.dumps(plan))
                with self.assertRaisesRegex(ValueError, expected):
                    runner.trace_rates(runner.load_plan(path))

    def test_drain_timeout_stops_sequence_with_point_result(self):
        status, out = self.execute(self.plan(drain_timeout=0.01))
        self.assertEqual(status, 1)
        record = json.loads((out / "slow/result.json").read_text())
        self.assertEqual(record["condition"], "drain_timeout")
        self.assertEqual(record["drain"]["condition"], "timeout")
        self.assertFalse((out / "fast").exists())
