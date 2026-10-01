"""Client trial accounting against synthetic HTTP responses and a fixed clock."""

import argparse
import asyncio
import io
import json
import tempfile
import threading
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
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
    async def test_drain_preserves_idle_timeout_and_state_error(self):
        def handler(request):
            if request.url.path != "/narwhal/state":
                return httpx.Response(404)
            return httpx.Response(200, json=IDLE)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            observed = await trial.poll_drain(client, "http://test", 1)
            self.assertEqual(observed["condition"], "idle")
            self.assertEqual(observed["polls"], 1)
            self.assertEqual(await trial.drain(client, "http://test", 1), IDLE)

        busy = IDLE | {"admission": IDLE["admission"] | {"inflight": 1}}
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=busy))
        ) as client:
            observed = await trial.poll_drain(client, "http://test", 0.01, 0.001)
            self.assertEqual(observed["condition"], "timeout")
            self.assertGreaterEqual(observed["polls"], 1)
            with self.assertRaisesRegex(ValueError, "Router drain deadline"):
                await trial.drain(client, "http://test", 0.01)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(503))
        ) as client:
            observed = await trial.poll_drain(client, "http://test", 1)
            self.assertEqual(observed["condition"], "state_error")
            with self.assertRaisesRegex(ValueError, "Router drain state error"):
                await trial.drain(client, "http://test", 1)

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

    async def test_one_token_stream_has_ttft_without_tpot(self):
        workload = WORKLOAD | {"output_tokens": 1}
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text=sse(count=1)))
        ) as client:
            row = await trial.request_one(
                client, "http://test", trial.body_for(workload, 0), "trial-0", 0, 10
            )
        self.assertEqual(row["outcome"], "completed")
        self.assertIsNotNone(row["ttft_s"])
        self.assertIsNone(row["tpot_s"])

    async def test_batched_token_event_counts_all_identified_tokens(self):
        content = sse().replace(
            'data: {"choices": [{"index": 0, "token_ids": [0], "text": ""}]}\n\n'
            'data: {"choices": [{"index": 0, "token_ids": [1], "text": ""}]}\n\n',
            'data: {"choices": [{"index": 0, "token_ids": [0, 1], "text": ""}]}\n\n',
        )
        times = iter([0.0, 2.0, 2.04, 2.1])
        row = await self.request(content, clock=lambda: next(times))
        self.assertEqual(row["outcome"], "completed")
        self.assertEqual(row["output_tokens"], 3)
        self.assertEqual(row["batched_token_events"], 1)
        self.assertAlmostEqual(row["tpot_s"], 0.02)

    async def test_truncation_and_wrong_usage_fail_validation(self):
        samples = [
            sse(done=False),
            sse(usage=False),
            sse(input_tokens=9),
            sse(count=2),
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
                run_seed=0,
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
                out=Path(directory),
                input_tokens=8192,
                output_tokens=128,
                seed=1729,
                prefix_tokens=None,
                families=None,
            )
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await trial.prepare(client, "http://test", args)
            workload = trial.load_workload(args.out / "workload.json")
            self.assertEqual(workload["token_pool"], list(range(32)))
            self.assertEqual(len(trial.body_for(workload, 0)["prompt"]), 8192)
            self.assertEqual(trial.body_for(workload, 0), trial.body_for(workload, 0))
            self.assertNotEqual(trial.body_for(workload, 0), trial.body_for(workload, 1))

    async def test_prepare_uses_prompt_ids_when_model_output_repeats(self):
        def handler(request):
            if request.url.path == "/v1/models":
                return httpx.Response(200, json={"data": [{"id": "test-model"}]})
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "prompt_token_ids": [11, 12, 13],
                            "token_ids": [7] * 32,
                            "finish_reason": "length",
                        }
                    ]
                },
            )

        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(
                out=Path(directory),
                input_tokens=8192,
                output_tokens=128,
                seed=1729,
                prefix_tokens=None,
                families=None,
            )
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await trial.prepare(client, "http://test", args)
            workload = trial.load_workload(args.out / "workload.json")
            self.assertEqual(workload["token_pool"], [11, 12, 13])

    async def test_prepare_rejects_a_workload_that_run_rejects(self):
        def handler(request):
            if request.url.path == "/v1/models":
                return httpx.Response(200, json={"data": [{"id": ""}]})
            return httpx.Response(
                200, json={"choices": [{"token_ids": list(range(32)), "finish_reason": "length"}]}
            )

        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(
                out=Path(directory),
                input_tokens=8192,
                output_tokens=128,
                seed=1729,
                prefix_tokens=None,
                families=None,
            )
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with self.assertRaisesRegex(ValueError, "Workload requires a model"):
                    await trial.prepare(client, "http://test", args)
            self.assertFalse((args.out / "seed-response.json").exists())
            self.assertFalse((args.out / "workload.json").exists())


@contextmanager
def local_router():
    """Serve a fake router on loopback and yield its base URL and the streamed prompts."""
    prompts = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            result = {"data": [{"id": "test-model"}]} if self.path == "/v1/models" else IDLE
            self.reply(json.dumps(result), "application/json")

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if body.get("stream"):
                prompts.append(tuple(body["prompt"]))
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
        yield f"http://127.0.0.1:{server.server_port}", prompts
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


class AccountingTests(unittest.TestCase):
    def test_cli_prepares_and_runs_through_a_local_http_server(self):
        with (
            local_router() as (base, _),
            tempfile.TemporaryDirectory() as directory,
            redirect_stdout(io.StringIO()),
        ):
            root = Path(directory)
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
            self.assertEqual(manifest["workload_sha256"], trial.digest(root / "seed/workload.json"))
            rows = [
                json.loads(row) for row in (root / "run/requests.jsonl").read_text().splitlines()
            ]
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(row["client_rid"].startswith(manifest["run_id"]) for row in rows))
            self.assertEqual((root / "run").stat().st_mode & 0o777, 0o700)

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

    def test_one_token_summary_uses_ttft_only(self):
        row = {
            "outcome": "completed",
            "ttft_s": 0.4,
            "tpot_s": None,
            "output_tokens": 1,
            "sent": True,
            "schedule_lag_s": 0.01,
        }
        args = argparse.Namespace(
            requests=1, max_lag=0.05, ttft=2, tpot=0.0333, attainment=0.95, rate=1
        )
        result = trial.summary([row], args, 1)
        self.assertTrue(result["candidate_pass"])
        self.assertIsNone(result["tpot_s"]["p95"])

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


class SharedPrefixWorkloadTests(unittest.TestCase):
    """Repeated-prefix prompts share family prefixes; cold controls share none."""

    def workload(self, families):
        return {
            **WORKLOAD,
            "kind": trial.SHARED_PREFIX,
            "input_tokens": 32,
            "prefix_tokens": 24,
            "families": families,
            "token_pool": list(range(100)),
        }

    def test_family_prefixes_repeat_across_runs_while_suffixes_do_not(self):
        workload = self.workload(2)
        prompts = [trial.prompt_for(workload, sequence) for sequence in range(12)]
        self.assertTrue(all(len(prompt) == 32 for prompt in prompts))
        self.assertEqual(len({tuple(prompt[:24]) for prompt in prompts}), 2)
        self.assertEqual(len({tuple(prompt[24:]) for prompt in prompts}), 12)
        rerun = [trial.prompt_for(workload, sequence, run_seed=1) for sequence in range(12)]
        self.assertEqual({tuple(p[:24]) for p in rerun}, {tuple(p[:24]) for p in prompts})
        self.assertTrue({tuple(p[24:]) for p in rerun}.isdisjoint({tuple(p[24:]) for p in prompts}))

    def test_cold_control_prefixes_are_unique_per_run_and_sequence(self):
        workload = self.workload(0)
        first = [tuple(trial.prompt_for(workload, s)[:24]) for s in range(12)]
        second = [tuple(trial.prompt_for(workload, s, run_seed=1)[:24]) for s in range(12)]
        self.assertEqual(len(set(first)), 12)
        self.assertTrue(set(first).isdisjoint(second))
        self.assertEqual(trial.body_for(workload, 0)["prompt"], list(trial.prompt_for(workload, 0)))

    def test_the_warmup_prefix_belongs_to_no_family(self):
        workload = self.workload(2)
        families = {tuple(trial.prompt_for(workload, s)[:24]) for s in range(12)}
        warmup = trial.warmup_body(workload, 12, 0)["prompt"]
        self.assertEqual(len(warmup), 32)
        self.assertNotIn(tuple(warmup[:24]), families)

    def test_shared_prefix_workloads_validate_their_shape(self):
        for changes in ({"prefix_tokens": 32}, {"prefix_tokens": 0}, {"families": -1}):
            with (
                self.subTest(changes=changes),
                tempfile.TemporaryDirectory() as folder,
                self.assertRaises(ValueError),
            ):
                path = Path(folder) / "workload.json"
                path.write_text(json.dumps({**self.workload(2), **changes}))
                trial.load_workload(path)


class SharedPrefixCliTests(unittest.TestCase):
    """`prepare` and `run` carry the shared-prefix shape and run seed to the router."""

    def prepare(self, base, out, families):
        with redirect_stdout(io.StringIO()):
            code = trial.main(
                [
                    *("prepare", "--base", base, "--out", str(out), "--input-tokens", "32"),
                    *("--output-tokens", "2", "--prefix-tokens", "24", "--families", str(families)),
                ]
            )
        self.assertEqual(code, 0)
        return out / "workload.json"

    def run_seed(self, base, out, workload, seed):
        with redirect_stdout(io.StringIO()):
            return trial.main(
                [
                    *("run", "--base", base, "--out", str(out), "--workload", str(workload)),
                    *("--requests", "8", "--rate", "100", "--max-lag", "1"),
                    *("--run-seed", str(seed)),
                ]
            )

    def test_family_prefixes_repeat_and_suffixes_differ_between_runs(self):
        with local_router() as (base, prompts), tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = self.prepare(base, root / "repeated", 2)
            workload = trial.load_workload(path)
            self.assertEqual(workload["kind"], trial.SHARED_PREFIX)
            self.assertEqual((workload["prefix_tokens"], workload["families"]), (24, 2))
            self.assertEqual(self.run_seed(base, root / "first", path, 1), 0)
            first = list(prompts)
            self.assertEqual(self.run_seed(base, root / "second", path, 2), 0)
            second = prompts[len(first) :]
        self.assertEqual(len(first), 9)
        self.assertEqual(len(second), 9)
        # Each run's first prompt is its warm-up, whose prefix belongs to no family.
        (warm_first, *first), (warm_second, *second) = first, second
        families = {p[:24] for p in first + second}
        self.assertEqual(len(families), 2)
        self.assertEqual({p[:24] for p in first}, {p[:24] for p in second})
        self.assertTrue({p[24:] for p in first}.isdisjoint({p[24:] for p in second}))
        self.assertTrue({warm_first[:24], warm_second[:24]}.isdisjoint(families))

    def test_cold_control_shares_no_prefix_between_requests_or_runs(self):
        with local_router() as (base, prompts), tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = self.prepare(base, root / "control", 0)
            self.assertEqual(self.run_seed(base, root / "first", path, 1), 0)
            self.assertEqual(self.run_seed(base, root / "second", path, 2), 0)
        self.assertEqual(len(prompts), 18)
        self.assertEqual(len({p[:16] for p in prompts}), 18)

    def test_run_seed_requires_a_shared_prefix_workload(self):
        with local_router() as (base, prompts), tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "workload.json"
            path.write_text(json.dumps(WORKLOAD))
            with redirect_stderr(io.StringIO()) as error:
                self.assertEqual(self.run_seed(base, root / "run", path, 1), 1)
        self.assertIn("--run-seed applies only to a shared-prefix workload", error.getvalue())
        self.assertEqual(prompts, [])

    def test_prefix_tokens_and_families_go_together(self):
        for extra in (["--prefix-tokens", "24"], ["--families", "2"]):
            with (
                self.subTest(extra=extra),
                tempfile.TemporaryDirectory() as folder,
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                trial.main(
                    [
                        *("prepare", "--base", "http://127.0.0.1:1", "--out", folder + "/w"),
                        *("--input-tokens", "32", *extra),
                    ]
                )

    def test_shared_prefix_workload_requires_a_diverse_pool(self):
        workload = {
            **WORKLOAD,
            "kind": trial.SHARED_PREFIX,
            "input_tokens": 32,
            "prefix_tokens": 24,
            "families": 2,
            "token_pool": list(range(trial.MIN_SHARED_POOL - 1)),
        }
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "workload.json"
            path.write_text(json.dumps(workload))
            with self.assertRaisesRegex(ValueError, "distinct pool token IDs"):
                trial.load_workload(path)
