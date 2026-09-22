"""Client trial accounting against synthetic HTTP responses and a fixed clock."""

import argparse
import asyncio
import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from tools.measurement import load_trial as trial

WORKLOAD = {
    "schema": 1,
    "kind": "synthetic-token-length",
    "model": "test-model",
    "input_tokens": 8,
    "output_tokens": 3,
    "seed": 1729,
    "token_pool": [5, 6, 7],
}
IDLE = {
    "admission": {"inflight": 0, "queued": 0, "waiting_prefill": 0, "waiting_decode": 0},
    "serving": {"http_retained": 0},
    "resident": {"e1": {"prefill": 0, "decode": 0}},
    "pinned": ["e1"],
}


def sse(*, count=3, input_tokens=8, done=True, usage=True):
    events = [{"choices": [{"index": 0, "token_ids": [i], "text": ""}]} for i in range(count)]
    events.append({"choices": [{"index": 0, "token_ids": [], "finish_reason": "length"}]})
    if usage:
        events.append(
            {"choices": [], "usage": {"prompt_tokens": input_tokens, "completion_tokens": count}}
        )
    return "".join("data: " + json.dumps(event) + "\n\n" for event in events) + (
        "data: [DONE]\n\n" if done else ""
    )


class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, content, *, status=200, clock=None):
        def handler(request):
            self.assertEqual(request.headers["x-request-id"], "trial-0")
            body = json.loads(request.content)
            self.assertEqual(body["min_tokens"], 3)
            self.assertTrue(body["ignore_eos"])
            self.assertFalse(body["add_special_tokens"])
            return httpx.Response(status, text=content)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await trial.request_one(
                client,
                "http://test",
                trial.body_for(WORKLOAD, 0),
                "trial-0",
                0,
                10,
                **({"clock": clock} if clock else {}),
            )

    async def test_latency_uses_identified_tokens_including_empty_text(self):
        times = iter([0.0, 2.0, 2.02, 2.04, 2.1])
        row = await self.request(sse(), clock=lambda: next(times))
        self.assertEqual(row["outcome"], "completed")
        self.assertEqual(row["input_tokens"], 8)
        self.assertEqual(row["output_tokens"], 3)
        self.assertEqual(row["ttft_s"], 2.0)
        self.assertAlmostEqual(row["tpot_s"], 0.02)

    async def test_truncation_wrong_usage_and_batched_tokens_fail_validation(self):
        samples = [
            sse(done=False),
            sse(usage=False),
            sse(input_tokens=9),
            sse(count=2),
            sse().replace('"token_ids": [0]', '"token_ids": [0, 1]'),
            sse().replace('"token_ids": [0]', '"token_ids": [true]'),
            'data: {"error": {"message": "engine failure"}}\n\n',
            "data: broken-json\n\n",
            sse(count=4),
        ]
        for content in samples:
            with self.subTest(content=content):
                row = await self.request(content)
                self.assertEqual(row["outcome"], "invalid_stream")

    async def test_refusal_is_retained_as_a_terminal_miss(self):
        row = await self.request('{"error":"refused"}', status=429)
        self.assertEqual(row["status"], 429)
        self.assertEqual(row["outcome"], "http_error")
        self.assertIn("refused", row["error_body"])

    async def test_timeout_terminates_the_whole_request(self):
        async def handler(request):
            await asyncio.sleep(1)
            return httpx.Response(200, text=sse())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            row = await trial.request_one(
                client, "http://test", trial.body_for(WORKLOAD, 0), "trial-0", 0, 0.01
            )
        self.assertEqual(row["outcome"], "timeout")

    async def test_open_loop_keeps_all_offers_when_client_is_full(self):
        async def handler(request):
            if request.url.path == "/narwhal/state":
                return httpx.Response(200, json=IDLE)
            await asyncio.sleep(0.025)
            return httpx.Response(200, text=sse())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workload = root / "workload.json"
            workload.write_text(json.dumps(WORKLOAD))
            out = root / "trial"
            out.mkdir()
            args = argparse.Namespace(
                workload=workload,
                out=out,
                requests=6,
                rate=200,
                timeout=1,
                max_lag=1,
                max_inflight=1,
                ttft=2,
                tpot=0.0333,
                attainment=0.95,
            )
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                self.assertEqual(await trial.run_trial(client, "http://test", args), 2)
            rows = [json.loads(line) for line in (out / "requests.jsonl").read_text().splitlines()]
            report = json.loads((out / "summary.json").read_text())
            self.assertEqual(len(rows), 6)
            self.assertEqual({r["sequence"] for r in rows}, set(range(6)))
            self.assertTrue(any(not row["sent"] for row in rows))
            self.assertFalse(report["client_schedule_valid"])
            self.assertEqual(report["offered"], 6)
            self.assertEqual(report["terminal"], 6)
            self.assertLess(report["attainment"], 1)
            for path in out.iterdir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    async def test_prepare_uses_model_output_ids_and_retains_frozen_workload(self):
        def handler(request):
            if request.url.path == "/v1/models":
                return httpx.Response(200, json={"data": [{"id": "test-model"}]})
            return httpx.Response(
                200, json={"choices": [{"token_ids": list(range(32)), "finish_reason": "length"}]}
            )

        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(
                out=Path(directory), input_tokens=8192, output_tokens=128, seed=1729
            )
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await trial.prepare(client, "http://test", args)
            workload = trial.load_workload(args.out / "workload.json")
            self.assertEqual(workload["token_pool"], list(range(32)))
            self.assertEqual(len(trial.body_for(workload, 0)["prompt"]), 8192)
            self.assertEqual(trial.body_for(workload, 0), trial.body_for(workload, 0))
            self.assertNotEqual(trial.body_for(workload, 0), trial.body_for(workload, 1))


class AccountingTests(unittest.TestCase):
    def test_cli_prepares_and_runs_through_a_local_http_server(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                result = {"data": [{"id": "test-model"}]} if self.path == "/v1/models" else IDLE
                self.reply(json.dumps(result), "application/json")

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if body.get("stream"):
                    self.reply(
                        sse(count=body["max_tokens"], input_tokens=len(body["prompt"])),
                        "text/event-stream",
                    )
                else:
                    self.reply(
                        json.dumps(
                            {"choices": [{"token_ids": list(range(32)), "finish_reason": "length"}]}
                        ),
                        "application/json",
                    )

            def reply(self, body, content_type):
                data = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
                root = Path(directory)
                base = f"http://127.0.0.1:{server.server_port}"
                self.assertEqual(
                    trial.main(
                        [
                            "prepare",
                            "--base",
                            base,
                            "--out",
                            str(root / "seed"),
                            "--input-tokens",
                            "8",
                            "--output-tokens",
                            "3",
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    trial.main(
                        [
                            "run",
                            "--base",
                            base,
                            "--out",
                            str(root / "run"),
                            "--workload",
                            str(root / "seed/workload.json"),
                            "--requests",
                            "2",
                            "--rate",
                            "100",
                            "--max-lag",
                            "1",
                        ]
                    ),
                    0,
                )
                manifest = json.loads((root / "run/manifest.json").read_text())
                self.assertEqual(manifest["helper_sha256"], trial.digest(Path(trial.__file__)))
                self.assertEqual(
                    manifest["workload_sha256"], trial.digest(root / "seed/workload.json")
                )
                rows = [
                    json.loads(row)
                    for row in (root / "run/requests.jsonl").read_text().splitlines()
                ]
                self.assertEqual(len(rows), 2)
                self.assertTrue(
                    all(row["client_rid"].startswith(manifest["run_id"]) for row in rows)
                )
                self.assertEqual((root / "run").stat().st_mode & 0o777, 0o700)
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def test_attainment_counts_failures_and_uses_joint_latency_limits(self):
        good = {
            "outcome": "completed",
            "ttft_s": 1,
            "tpot_s": 0.02,
            "output_tokens": 128,
            "sent": True,
            "schedule_lag_s": 0.01,
        }
        rows = [
            good,
            good | {"tpot_s": 0.04},
            good | {"outcome": "http_error"},
            good | {"ttft_s": 3},
        ]
        args = argparse.Namespace(
            requests=4, max_lag=0.05, ttft=2, tpot=0.0333, attainment=0.95, rate=1
        )
        result = trial.summary(rows, args, 8)
        self.assertEqual(result["attainment"], 0.25)
        self.assertEqual(result["completed_rps_including_drain"], 3 / 8)
        self.assertEqual(result["qualified_rps_including_drain"], 1 / 8)
        self.assertFalse(result["candidate_pass"])
        self.assertEqual(trial.summary(rows[:1], args, 8)["attainment"], 0.25)
        self.assertFalse(trial.summary(rows[:1], args, 8)["client_schedule_valid"])

    def test_drain_checks_resident_and_admission_work_and_allows_role_pins(self):
        self.assertTrue(trial.idle(IDLE))
        self.assertFalse(trial.idle(IDLE | {"resident": {"e1": {"prefill": 0, "decode": 1}}}))
        self.assertFalse(trial.idle(IDLE | {"admission": IDLE["admission"] | {"queued": 1}}))

    def test_rejects_invalid_workload_and_preserves_existing_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "workload.json"
            trial.private_json(path, WORKLOAD | {"token_pool": [True]})
            with self.assertRaises(ValueError):
                trial.load_workload(path)
            with self.assertRaises(FileExistsError):
                trial.private_json(path, WORKLOAD)


if __name__ == "__main__":
    unittest.main()
