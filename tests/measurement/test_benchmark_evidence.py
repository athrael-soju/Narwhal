"""Integrate the evidence collector with a live local runner and fake telemetry."""

import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tools.measurement import benchmark_runner

CLIENT = """
import json, pathlib, sys, time, urllib.request
base, directory, mode, model = sys.argv[1:]
rows = []
if mode == 'warmup':
    urllib.request.urlopen(base + '/start/warmup').read()
    target = pathlib.Path(directory) / 'client'
    target.mkdir()
    (target / 'warmup.json').write_text(json.dumps(
        {'client_rid': 'warmup', 'sent': True, 'outcome': 'completed'}))
for name in ('a', 'b') if mode == 'restart' else ('a',):
    if name == 'b':
        urllib.request.urlopen(base + '/restart').read()
        time.sleep(0.2)
    urllib.request.urlopen(base + '/start/' + name).read()
    rows.append({'client_rid': name, 'sent': True, 'outcome': 'completed'})
    time.sleep(0.2)
target = pathlib.Path(directory) / 'client'
target.mkdir(exist_ok=True)
(target / 'requests.jsonl').write_text(''.join(json.dumps(row) + '\\n' for row in rows))
"""


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.journal = self.root / "journal.jsonl"
        self.journal.write_text("")
        (self.root / "fleet.json").write_text('{"fleet":"test"}')
        (self.root / "profiles.json").write_text('{"profiles":"test"}')
        self.run_id = "run-a"
        self.offered = 0
        self.served = 0
        self.flips = 0
        self.flip_history = []
        self.pools = {"prefill": ["e0"], "decode": ["e1"]}
        self.missing_journal = False
        self.fail_engine = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                status = 200
                if self.path == "/ready":
                    body = {"status": "ready"}
                elif self.path == "/v1/models":
                    body = {"data": [{"id": "test-model"}]}
                elif self.path == "/narwhal/state":
                    body = {
                        "journal_run": outer.run_id,
                        "admission": {
                            "inflight": 0,
                            "queued": 0,
                            "waiting_prefill": 0,
                            "waiting_decode": 0,
                        },
                        "serving": {"http_retained": 0},
                        "resident": {"e0": {"prefill": 0, "decode": 0}},
                        "pools": outer.pools,
                        "flips": outer.flip_history,
                    }
                elif self.path == "/metrics":
                    body = (
                        "\n".join(
                            f"{name} {value}"
                            for name, value in {
                                "narwhal_offered_total": outer.offered,
                                "narwhal_served_total": outer.served,
                                "narwhal_failed_total": 0,
                                "narwhal_refused_total": 0,
                                "narwhal_rejected_total": 0,
                                "narwhal_expired_total": 0,
                                "narwhal_invalid_requests_total": 0,
                                "narwhal_cancelled_total": 0,
                                'narwhal_flips_total{to="prefill",by="reactive"}': outer.flips,
                            }.items()
                        )
                        + "\n"
                    )
                elif self.path == "/engine-metrics":
                    status = 503 if outer.fail_engine else 200
                    body = "engine_running_requests 0\n"
                elif self.path == "/restart":
                    outer.run_id = "run-b"
                    outer.offered = 0
                    outer.flips = 0
                    outer.flip_history = []
                    body = {"ok": True}
                elif self.path.startswith("/start/"):
                    name = self.path.rsplit("/", 1)[1]
                    outer.offered += 1
                    outer.served += 1
                    if name == "a":
                        outer.flips += 1
                        outer.pools = {"prefill": ["e1"], "decode": ["e0"]}
                        outer.flip_history = [
                            {"at": 1.0, "iid": "e1", "to": "prefill", "by": "reactive"}
                        ]
                    if not outer.missing_journal:
                        with outer.journal.open("a") as file:
                            file.write(
                                json.dumps(
                                    {
                                        "run": outer.run_id,
                                        "client_rid": name,
                                        "terminal": "completed",
                                    }
                                )
                                + "\n"
                            )
                    body = {"ok": True}
                else:
                    status, body = 404, {"error": "unknown path"}
                data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.worker.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def execute(self, mode="normal"):
        plan = {
            "schema": 1,
            "evidence": {
                "journal_path": str(self.journal),
                "fleet_path": str(self.root / "fleet.json"),
                "profiles_path": str(self.root / "profiles.json"),
                "sample_interval_s": 0.05,
                "engine_metrics_urls": {"e0": self.base + "/engine-metrics"},
                "identity": {
                    "narwhal_revision": "test-revision",
                    "model_id": "test-model",
                    "benchmark_client_version": "test-client",
                    "engine_image": "private.registry/engine:test",
                    "engine_version": "test",
                    "checkpoint_revision": "test-checkpoint",
                    "gpu_shape": "test-gpu",
                    "gpu_allocation": "2",
                    "initial_role_split": "1P/1D",
                },
            },
            "points": [
                {
                    "id": "point-a",
                    "workload": {"rate_rps": 1, "requests": 2 if mode == "restart" else 1},
                    "client_argv": [
                        sys.executable,
                        "-c",
                        CLIENT,
                        "{base}",
                        "{point_dir}",
                        mode,
                        "{model}",
                    ],
                    "client_timeout_s": 5,
                    "drain_timeout_s": 2,
                }
            ],
        }
        plan_path = self.root / "plan.json"
        plan_path.write_text(json.dumps(plan))
        out = self.root / "run"
        status = benchmark_runner.main(
            [
                "--base",
                self.base,
                "--model",
                "test-model",
                "--plan",
                str(plan_path),
                "--out",
                str(out),
            ]
        )
        return status, json.loads((out / "point-a/evidence.json").read_text()), out

    def test_role_change_and_counts_are_reconciled(self):
        status, evidence, out = self.execute()
        self.assertEqual(status, 0)
        self.assertEqual(evidence["diagnostics"], [])
        self.assertEqual(evidence["journal"]["outcomes"], {"completed": 1})
        self.assertEqual(evidence["counter_deltas_by_run"]["run-a"]["narwhal_offered_total"], 1)
        self.assertTrue(any("flip" in item for item in evidence["role_timeline"]))
        shareable = (out / "point-a/summary.shareable.json").read_text()
        self.assertNotIn(self.base, shareable)
        self.assertNotIn("private.registry", shareable)

    def test_two_runs_use_separate_counter_boundaries(self):
        status, evidence, _ = self.execute("restart")
        self.assertEqual(status, 0)
        self.assertEqual(evidence["diagnostics"], [])
        self.assertEqual(evidence["journal"]["runs"], ["run-a", "run-b"])
        self.assertEqual(evidence["counter_deltas_by_run"]["run-a"]["narwhal_offered_total"], 1)
        self.assertEqual(evidence["counter_deltas_by_run"]["run-b"]["narwhal_offered_total"], 1)
        self.assertEqual(evidence["counter_deltas_by_run"]["run-b"]["narwhal_served_total"], 1)

    def test_warmup_is_reconciled_but_excluded_from_measured_counts(self):
        status, evidence, _ = self.execute("warmup")
        self.assertEqual(status, 0)
        self.assertEqual(evidence["diagnostics"], [])
        self.assertEqual(evidence["client"]["sent"], 2)
        self.assertEqual(evidence["client"]["warmup_sent"], 1)
        self.assertEqual(evidence["client"]["measured_sent"], 1)
        self.assertEqual(evidence["journal"]["terminal"], 2)

    def test_discrepancy_and_scrape_gap_are_tied_to_point(self):
        self.missing_journal = True
        self.fail_engine = True
        _, evidence, _ = self.execute()
        kinds = {item["kind"] for item in evidence["diagnostics"]}
        self.assertIn("client_journal_count", kinds)
        self.assertIn("scrape_error", kinds)
        self.assertTrue(all(item.get("point") == "point-a" for item in evidence["diagnostics"]))
