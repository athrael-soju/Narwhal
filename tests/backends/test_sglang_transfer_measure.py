"""The SGLang transfer measurement tool sizes fabric budgets from measured KV handoffs."""

import contextlib
import hashlib
import io
import json
import math
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from narwhal.backends import load
from narwhal.backends.sglang.transfer_measure import main, transfer_payload
from narwhal.deployment.fabric_budget import TRANSFER_SCHEMA
from narwhal.deployment.launch_engine.backend import transfer_measurement


class TransferMeasurementTests(unittest.TestCase):
    def test_the_sglang_launcher_delivers_a_standalone_measurement_tool(self):
        measurement = transfer_measurement(load("sglang").engine_launcher())
        self.assertIsNotNone(measurement)
        assert measurement is not None
        for name in ("vllm", "trtllm"):
            self.assertIsNone(transfer_measurement(load(name).engine_launcher()), name)
        with tempfile.TemporaryDirectory() as folder:
            copy = Path(folder) / "transfer_measure.py"
            copy.write_bytes(measurement.transfer_tool.read_bytes())
            result = subprocess.run(
                [sys.executable, "-I", str(copy), "--help"], capture_output=True, text=True
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--out-dir", result.stdout)

    def test_measured_sglang_transfers_sum_each_rank_mean(self):
        def metrics(rank0: tuple[float, int], rank1: tuple[float, int]) -> str:
            lines = ["# TYPE sglang:kv_transfer_total_mb histogram"]
            for rank, (mib, count) in enumerate((rank0, rank1)):
                labels = f'{{engine_type="dynamic",tp_rank="{rank}"}}'
                lines += [
                    f'sglang:kv_transfer_total_mb_bucket{{le="1.0",tp_rank="{rank}"}} {count}',
                    f"sglang:kv_transfer_total_mb_sum{labels} {mib}",
                    f"sglang:kv_transfer_total_mb_count{labels} {count}",
                ]
            return "\n".join(lines) + "\n"

        before = metrics((1.0, 1), (1.0, 1))
        after = metrics((7.0, 5), (5.0, 5))
        self.assertEqual(transfer_payload(before, after), (math.ceil(2.5 * 1024 * 1024), 4))
        self.assertEqual(transfer_payload("", metrics((3.0, 2), (3.0, 2)))[1], 2)
        for start, end in (
            (before, "sglang:num_running_reqs 0\n"),
            (before, before),
            (before, metrics((7.0, 5), (5.0, 4))),
        ):
            with self.subTest(end=end), self.assertRaises(ValueError):
                transfer_payload(start, end)

    def test_measure_transfer_sends_cold_paired_legs_and_saves_the_snapshots(self):
        for found, switches in (
            ("prefill", [("decode", 4.0), ("prefill", None)]),
            ("decode", [("prefill", None), ("decode", 2.5)]),
        ):
            with self.subTest(found=found):
                self._measure_transfer(found, switches)

    def _measure_transfer(self, found: str, switches: list) -> None:
        bodies: dict[str, list[dict]] = {"prefill": [], "decode": []}
        modes = {"prefill": "prefill", "decode": found}
        switched: list[tuple[str, float | None]] = []

        def engine(role: str) -> ThreadingHTTPServer:
            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    if self.path == "/server_info":
                        state = {"disaggregation_mode": modes[role]}
                        if modes[role] == "decode":
                            state["decode_cuda_graph_memory_gb"] = 2.5
                        self._reply(json.dumps({"internal_states": [state]}).encode())
                        return
                    count = len(bodies["prefill"])
                    text = (
                        f'sglang:kv_transfer_total_mb_sum{{tp_rank="0"}} {2.0 * count}\n'
                        f'sglang:kv_transfer_total_mb_count{{tp_rank="0"}} {count}\n'
                    )
                    self._reply(text.encode())

                def do_POST(self):
                    length = int(self.headers["Content-Length"])
                    body = json.loads(self.rfile.read(length))
                    if self.path == "/pd_role_switch":
                        switched.append((body["new_role"], body.get("decode_cuda_graph_memory_gb")))
                        modes[role] = body["new_role"]
                        self._reply(b'{"success": true}')
                        return
                    bodies[role].append(body)
                    self._reply(b"{}")

                def _reply(self, data: bytes) -> None:
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)

                def log_message(self, *args):
                    pass

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            return server

        prefill, decode = engine("prefill"), engine("decode")
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / "transfer"
            launch = Path(folder) / "launch.json"
            launch.write_text(json.dumps({"runtime": {"decode_cuda_graph_memory_gb": 4.0}}))
            args = [
                "--prefill",
                f"http://127.0.0.1:{prefill.server_port}",
                "--decode",
                f"http://127.0.0.1:{decode.server_port}",
                "--bootstrap-host",
                "prefill.invalid",
                "--bootstrap-port",
                "8998",
                "--model",
                "model",
                "--launch-config",
                str(launch),
                "--prompt-tokens",
                "300",
                "--handoffs",
                "3",
                "--out-dir",
                str(out),
            ]
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(main(args), 0)
            self.assertEqual(
                json.loads(stdout.getvalue()),
                {
                    "transfer_record": str(out / "transfer.json"),
                    "payload_bytes_per_handoff": 2 * 1024 * 1024,
                    "handoffs": 3,
                },
            )
            self.assertEqual(
                sorted(path.name for path in out.iterdir()),
                ["after.prom", "before.prom", "transfer.json"],
            )
            record = json.loads((out / "transfer.json").read_text())
            self.assertEqual((out / "transfer.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                record["metrics_sha256"],
                {
                    name: hashlib.sha256((out / name).read_bytes()).hexdigest()
                    for name in ("before.prom", "after.prom")
                },
            )
            self.assertEqual(
                {key: record[key] for key in ("schema", "series", "prompt_tokens", "handoffs")},
                {
                    "schema": TRANSFER_SCHEMA,
                    "series": "sglang:kv_transfer_total_mb",
                    "prompt_tokens": 300,
                    "handoffs": 3,
                },
            )
        self.assertEqual(switched, switches)
        self.assertEqual(modes["decode"], found)
        self.assertEqual(bodies["prefill"], bodies["decode"])
        self.assertEqual(len({body["bootstrap_room"] for body in bodies["prefill"]}), 3)
        for body in bodies["prefill"]:
            self.assertEqual(len(body["prompt"]), 300)
            self.assertEqual(
                (body["bootstrap_host"], body["bootstrap_port"]), ("prefill.invalid", 8998)
            )
            self.assertEqual(body["max_tokens"], 1)
