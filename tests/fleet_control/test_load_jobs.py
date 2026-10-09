"""AIPerf load jobs: the workload library, command lines, result parsing and job control."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import httpx

from tools.fleet_control import cli as control_cli
from tools.fleet_control.aiperf import (
    AIPerfRunner,
    command,
    runner_for,
    summarize,
    workload_routes,
)
from tools.fleet_control.app import create_app
from tools.fleet_control.config import (
    ConfigError,
    ControlConfig,
    Hook,
    RouterEndpoint,
    load_config,
)
from tools.fleet_control.service import ControlService
from tools.fleet_control.workloads import LoadConfig, Workload, validate_params

ROOT = Path(__file__).resolve().parents[2]
FLEET = ROOT / "tests/data/fleet.json"
TOKEN = "fake-control-token-" + "0" * 24
URL = "http://127.0.0.1:8000"
ROUTER = "http://127.0.0.1:18000"

# The fields of an AIPerf 0.13.0 `profile_export_aiperf.json` that the runner reads.
SUMMARY: dict[str, Any] = {
    "schema_version": "1.4",
    "aiperf_version": "0.13.0",
    "was_cancelled": False,
    "is_complete": True,
    "error_summary": [
        {
            "error_details": {"code": 503, "type": "HTTPError", "message": "unavailable"},
            "count": 2,
        },
        {"error_details": {"code": None, "type": "TimeoutError", "message": "late"}, "count": 1},
    ],
    "request_count": {"unit": "requests", "avg": 97.0},
    "error_request_count": {"unit": "requests", "avg": 3.0},
    "request_throughput": {"unit": "requests/sec", "avg": 9.7},
    "output_token_throughput": {"unit": "tokens/sec", "avg": 2483.2},
    "goodput": {"unit": "requests/sec", "avg": 9.1},
    "good_request_count": {"unit": "requests", "avg": 91.0},
    "time_to_first_token": {
        "unit": "ms",
        "avg": 120.5,
        "p1": 80.0,
        "p50": 110.0,
        "p90": 180.0,
        "p95": 200.0,
        "p99": 250.0,
        "min": 75.0,
        "max": 260.0,
        "std": 30.0,
    },
    "inter_token_latency": {"unit": "ms", "avg": 12.0, "p50": 11.0, "p90": 15.0, "p99": 20.0},
    "request_latency": {"unit": "ms", "avg": 3200.0, "p50": 3100.0, "p99": 4500.0},
}


def aiperf_record(rid: str, phase: str = "profiling", error: dict[str, Any] | None = None) -> str:
    row = {
        "metadata": {"x_request_id": rid, "benchmark_phase": phase, "was_cancelled": False},
        "metrics": {"request_latency": {"value": 1.0, "unit": "ms"}},
        "error": error,
    }
    return json.dumps(row) + "\n"


RECORDS = (
    aiperf_record("warmup", "warmup")
    + "".join(aiperf_record(f"ok-{n}") for n in range(97))
    + aiperf_record("busy-1", error={"code": 503, "type": "HTTPError", "message": "x"})
    + aiperf_record("busy-2", error={"code": 503, "type": "HTTPError", "message": "x"})
    + aiperf_record("late", error={"code": None, "type": "TimeoutError", "message": "x"})
)

EXPECTED_REQUESTS = {
    "total": 100,
    "completed": 97,
    "errors": 3,
    "errors_by_type": {"TimeoutError": 1, "http_503": 2},
    "source": "profile_export.jsonl",
}

FAKE_AIPERF = """#!{python}
import json, os, pathlib, shutil, subprocess, sys, time
argv = sys.argv[1:]
artifacts = pathlib.Path(argv[argv.index("--artifact-dir") + 1])
state = pathlib.Path(os.environ["FAKE_AIPERF_STATE"])
(state / "argv.json").write_text(json.dumps(argv))
(state / "env.json").write_text(json.dumps(sorted(os.environ)))
mode = os.environ["FAKE_AIPERF_MODE"]
print("aiperf starting", flush=True)
if mode == "hang":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    (state / "pids.json").write_text(json.dumps([os.getpid(), child.pid]))
    time.sleep(60)
if mode == "fail":
    print("tokenizer not found", file=sys.stderr, flush=True)
    sys.exit(3)
if mode != "no-summary":
    shutil.copy(state / "summary.json", artifacts / "profile_export_aiperf.json")
    shutil.copy(state / "records.jsonl", artifacts / "profile_export.jsonl")
time.sleep(float(os.environ["FAKE_AIPERF_SLEEP"]))
"""

SYNTHETIC = Workload("chat-512-256", "synthetic", isl=512, osl=256, osl_stddev=16.0)
TIMESTAMPED = Workload("replay", "timestamped_trace", file=Path("/traces/replay.jsonl"))
PREFIX = Workload("prefix", "prefix_trace", file=Path("/traces/prefix.jsonl"), block_size=512)


def load_config_for(aiperf: str, **overrides: Any) -> LoadConfig:
    settings: dict[str, Any] = {
        "aiperf": aiperf,
        "model": "test-model",
        "tokenizer": "/tokenizers/test-model",
        "workloads": {w.name: w for w in (SYNTHETIC, TIMESTAMPED, PREFIX)},
    }
    settings.update(overrides)
    return LoadConfig(**settings)


class LoadConfigTests(unittest.TestCase):
    def write(self, folder: str, load: object) -> Path:
        path = Path(folder) / "fleet-control.local.json"
        document = {"fleet": str(FLEET), "hooks": {"restore": {"argv": ["x"]}}, "load": load}
        path.write_text(json.dumps(document))
        return path

    def test_example_config_defines_each_workload_kind(self) -> None:
        config = load_config(ROOT / "config/fleet-control.example.json", {})
        assert config.load is not None
        kinds = {workload.kind for workload in config.load.workloads.values()}
        self.assertEqual(kinds, {"synthetic", "timestamped_trace", "prefix_trace"})
        self.assertEqual(config.load.endpoint_type, "chat")
        self.assertTrue(config.load.streaming)
        self.assertEqual(
            config.load.goodput, {"time_to_first_token": 10000.0, "inter_token_latency": 125.0}
        )

    def test_load_jobs_are_optional(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "control.json"
            path.write_text(
                json.dumps({"fleet": str(FLEET), "hooks": {"restore": {"argv": ["x"]}}})
            )
            self.assertIsNone(load_config(path, {}).load)

    def test_defaults_and_library_entries(self) -> None:
        load = {
            "aiperf": "aiperf",
            "model": "m",
            "tokenizer": "t",
            "workloads": {
                "short": {"kind": "synthetic", "isl": 128, "osl": 64, "isl_stddev": 8},
                "replay": {"kind": "timestamped_trace", "file": "traces/replay.jsonl"},
            },
        }
        with tempfile.TemporaryDirectory() as folder:
            config = load_config(self.write(folder, load), {})
        assert config.load is not None
        self.assertEqual(config.load.endpoint_type, "chat")
        self.assertTrue(config.load.streaming)
        self.assertEqual(config.load.goodput, {})
        self.assertIsNone(config.load.grace_period_s)
        self.assertEqual(config.load.extra_args, ())
        self.assertEqual(
            config.load.workloads["short"],
            Workload("short", "synthetic", isl=128, osl=64, isl_stddev=8.0),
        )
        self.assertEqual(
            config.load.workloads["replay"],
            Workload("replay", "timestamped_trace", file=Path("traces/replay.jsonl")),
        )

    def test_every_load_problem_is_reported(self) -> None:
        load = {
            "aiperf": "",
            "model": "m",
            "tokenizer": "t",
            "endpoint_type": "embeddings",
            "streaming": "yes",
            "goodput": {"time_to_first_token": -1},
            "grace_period_s": -5,
            "extra_args": ["--ok", 3],
            "rate": 4,
            "workloads": {
                "bad name": {"kind": "synthetic", "isl": 1, "osl": 1},
                "no-kind": {"isl": 1},
                "zero": {"kind": "synthetic", "isl": 0, "osl": 1.5, "osl_stddev": -1},
                "prefix": {"kind": "prefix_trace", "file": "p.jsonl"},
                "replay": {"kind": "timestamped_trace", "file": "", "isl": 3},
            },
        }
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(ConfigError) as caught:
            load_config(self.write(folder, load), {})
        message = str(caught.exception)
        for problem in (
            "unknown key load.rate",
            "load.aiperf must be a non-empty string",
            "load.endpoint_type must be one of chat, completions",
            "load.streaming must be true or false",
            "load.goodput must map AIPerf metric tags to positive numbers",
            "load.grace_period_s must be a non-negative number of seconds",
            "load.extra_args must be a list of non-empty strings",
            "load.workloads.bad name: names use letters",
            "load.workloads.no-kind.kind must be one of synthetic, timestamped_trace, prefix_trace",
            "load.workloads.zero.isl must be a positive number of tokens",
            "load.workloads.zero.osl must be a positive number of tokens",
            "load.workloads.zero.osl_stddev must be a non-negative number of tokens",
            "load.workloads.prefix.block_size is required",
            "unknown key load.workloads.replay.isl",
            "load.workloads.replay.file must name a mooncake_trace JSONL file",
        ):
            self.assertIn(problem, message)

    def test_an_empty_library_is_refused(self) -> None:
        load = {"aiperf": "a", "model": "m", "tokenizer": "t", "workloads": {}}
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ConfigError, "load.workloads must be an object naming"):
                load_config(self.write(folder, load), {})
            with self.assertRaisesRegex(ConfigError, "load must be an object"):
                load_config(self.write(folder, ["aiperf"]), {})


class ParameterTests(unittest.TestCase):
    workloads = load_config_for("aiperf").workloads

    def validate(self, **params: Any) -> dict[str, Any]:
        return validate_params(self.workloads, params)

    def refused(self, message: str, **params: Any) -> None:
        with self.assertRaises(ValueError) as caught:
            self.validate(**params)
        self.assertIn(message, str(caught.exception))

    def test_synthetic_workloads_need_a_duration_and_pacing(self) -> None:
        self.assertEqual(
            self.validate(workload="chat-512-256", rate=2.5, duration_s=60),
            {"workload": "chat-512-256", "kind": "synthetic", "rate": 2.5, "duration_s": 60},
        )
        self.assertEqual(
            self.validate(workload="chat-512-256", concurrency=8, rate=4, duration_s=30)["kind"],
            "synthetic",
        )
        self.refused(
            "synthetic workload 'chat-512-256' needs duration_s", workload="chat-512-256", rate=1
        )
        self.refused(
            "synthetic workload 'chat-512-256' needs a rate, a concurrency or both",
            workload="chat-512-256",
            duration_s=10,
        )

    def test_timestamped_traces_take_no_rate(self) -> None:
        self.assertEqual(
            self.validate(workload="replay"), {"workload": "replay", "kind": "timestamped_trace"}
        )
        self.assertEqual(
            self.validate(workload="replay", concurrency=64, duration_s=120),
            {
                "workload": "replay",
                "kind": "timestamped_trace",
                "concurrency": 64,
                "duration_s": 120,
            },
        )
        self.refused(
            "timestamped_trace workload 'replay' replays its recorded timestamps and takes no rate",
            workload="replay",
            rate=3,
        )

    def test_prefix_traces_are_paced_by_the_job(self) -> None:
        self.assertEqual(
            self.validate(workload="prefix", concurrency=16, duration_s=300),
            {"workload": "prefix", "kind": "prefix_trace", "concurrency": 16, "duration_s": 300},
        )
        self.refused("prefix_trace workload 'prefix' needs duration_s", workload="prefix", rate=2)
        self.refused(
            "prefix_trace workload 'prefix' needs a rate, a concurrency or both",
            workload="prefix",
            duration_s=300,
        )

    def test_parameter_types_and_names_are_checked(self) -> None:
        self.refused("workload must name a library entry: chat-512-256, prefix, replay")
        self.refused("workload must name a library entry", workload="missing")
        self.refused("unknown job parameter 'requests'", workload="replay", requests=10)
        for value in (0, -1, "2", True, float("inf")):
            with self.subTest(rate=value):
                self.refused("rate must be a positive number", workload="prefix", rate=value)
        for value in (0, 1.5, True):
            with self.subTest(concurrency=value):
                self.refused(
                    "concurrency must be a positive integer", workload="replay", concurrency=value
                )
        self.refused("duration_s must be a positive number", workload="replay", duration_s=0)


COMMON = (
    "/opt/aiperf/bin/aiperf",
    "profile",
    "--url",
    URL,
    "--model",
    "test-model",
    "--tokenizer",
    "/tokenizers/test-model",
    "--endpoint-type",
    "chat",
    "--streaming",
    "--ui",
    "none",
    "--no-gpu-telemetry",
    "--export-level",
    "records",
    "--artifact-dir",
    "/runs/job-001/aiperf",
)
TAIL = (
    "--goodput",
    "time_to_first_token:10000 inter_token_latency:125",
    "--extra-inputs",
    "ignore_eos:true",
)


class CommandTests(unittest.TestCase):
    load = load_config_for(
        "/opt/aiperf/bin/aiperf",
        goodput={"time_to_first_token": 10000.0, "inter_token_latency": 125.0},
        grace_period_s=30.0,
        extra_args=("--extra-inputs", "ignore_eos:true"),
    )
    artifacts = Path("/runs/job-001/aiperf")

    def test_synthetic_workload(self) -> None:
        params = {"rate": 2.5, "concurrency": 32, "duration_s": 300}
        self.assertEqual(
            command(self.load, URL, SYNTHETIC, params, self.artifacts),
            [
                *COMMON,
                *("--isl", "512", "--isl-stddev", "0", "--osl", "256", "--osl-stddev", "16"),
                *("--request-rate", "2.5", "--concurrency", "32"),
                *("--benchmark-duration", "300", "--benchmark-grace-period", "30"),
                *TAIL,
            ],
        )

    def test_timestamped_trace_replays_its_schedule(self) -> None:
        self.assertEqual(
            command(self.load, URL, TIMESTAMPED, {}, self.artifacts),
            [
                *COMMON,
                *(
                    "--input-file",
                    "/traces/replay.jsonl",
                    "--custom-dataset-type",
                    "mooncake_trace",
                ),
                *("--fixed-schedule", "--fixed-schedule-auto-offset"),
                *TAIL,
            ],
        )
        capped = command(
            self.load, URL, TIMESTAMPED, {"concurrency": 8, "duration_s": 60}, self.artifacts
        )
        self.assertEqual(
            capped[len(COMMON) + 6 : -len(TAIL)],
            ["--concurrency", "8", "--benchmark-duration", "60", "--benchmark-grace-period", "30"],
        )

    def test_prefix_trace_is_paced_with_its_block_size(self) -> None:
        params = {"rate": 4, "duration_s": 120}
        self.assertEqual(
            command(self.load, URL, PREFIX, params, self.artifacts),
            [
                *COMMON,
                *(
                    "--input-file",
                    "/traces/prefix.jsonl",
                    "--custom-dataset-type",
                    "mooncake_trace",
                ),
                *("--no-fixed-schedule", "--isl-block-size", "512"),
                *("--request-rate", "4"),
                *("--benchmark-duration", "120", "--benchmark-grace-period", "30"),
                *TAIL,
            ],
        )

    def test_optional_settings_are_omitted(self) -> None:
        load = load_config_for("aiperf", streaming=False, endpoint_type="completions")
        argv = command(load, URL, SYNTHETIC, {"concurrency": 1, "duration_s": 5}, self.artifacts)
        self.assertNotIn("--streaming", argv)
        self.assertNotIn("--goodput", argv)
        self.assertNotIn("--benchmark-grace-period", argv)
        self.assertEqual(argv[argv.index("--endpoint-type") + 1], "completions")
        self.assertEqual(argv[-4:], ["--concurrency", "1", "--benchmark-duration", "5"])


class SummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.artifacts = Path(folder.name) / "aiperf"
        self.artifacts.mkdir()

    def test_summary_and_records_reduce_to_the_job_result(self) -> None:
        (self.artifacts / "profile_export_aiperf.json").write_text(json.dumps(SUMMARY))
        (self.artifacts / "profile_export.jsonl").write_text(RECORDS + "not json\n")
        result = summarize(self.artifacts, {"time_to_first_token": 10000.0})
        self.assertEqual(result["summary"], "aiperf/profile_export_aiperf.json")
        self.assertEqual(result["aiperf_version"], "0.13.0")
        self.assertIs(result["is_complete"], True)
        self.assertIs(result["was_cancelled"], False)
        self.assertEqual(result["requests"], {**EXPECTED_REQUESTS, "unreadable_records": 1})
        self.assertEqual(
            result["throughput"], {"requests_per_s": 9.7, "output_tokens_per_s": 2483.2}
        )
        self.assertEqual(
            result["goodput"],
            {"slo": {"time_to_first_token": 10000.0}, "requests_per_s": 9.1, "good_requests": 91},
        )
        self.assertEqual(
            result["latency"]["ttft"],
            {"unit": "ms", "avg": 120.5, "p50": 110.0, "p90": 180.0, "p95": 200.0, "p99": 250.0},
        )
        self.assertEqual(
            result["latency"]["itl"],
            {"unit": "ms", "avg": 12.0, "p50": 11.0, "p90": 15.0, "p99": 20.0},
        )
        self.assertEqual(result["latency"]["request"]["p99"], 4500.0)

    def test_errors_come_from_the_summary_without_records(self) -> None:
        nested = self.artifacts / "profile-run"
        nested.mkdir()
        (nested / "profile_export_aiperf.json").write_text(json.dumps(SUMMARY))
        result = summarize(self.artifacts, {})
        self.assertEqual(result["summary"], "aiperf/profile-run/profile_export_aiperf.json")
        self.assertEqual(
            result["requests"], {**EXPECTED_REQUESTS, "source": "profile_export_aiperf.json"}
        )
        self.assertNotIn("goodput", result)

    def test_a_missing_summary_is_an_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "no profile_export_aiperf.json under aiperf/"):
            summarize(self.artifacts, {})


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        # An orphan stays a zombie until its new parent reaps it.
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1][0] != "Z"
    except FileNotFoundError:
        return False


class RunnerCase(unittest.IsolatedAsyncioTestCase):
    """Serve the control app with the AIPerf runner and a fake `aiperf` executable."""

    mode = "complete"
    sleep = 0.0

    async def asyncSetUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.state = root / "state"
        self.state.mkdir()
        (self.state / "summary.json").write_text(json.dumps(SUMMARY))
        (self.state / "records.jsonl").write_text(RECORDS)
        self.aiperf = root / "aiperf"
        self.aiperf.write_text(FAKE_AIPERF.replace("{python}", sys.executable))
        self.aiperf.chmod(0o755)
        self.trace = root / "replay.jsonl"
        self.trace.write_text(
            '{"timestamp": 0, "input_length": 600, "output_length": 20, "hash_ids": [1, 2]}\n'
        )
        self.runs = root / "runs"
        replay = Workload("replay", "timestamped_trace", file=self.trace, block_size=300)
        load = load_config_for(
            self.executable(),
            workloads={"chat-512-256": SYNTHETIC, "replay": replay},
            goodput={"time_to_first_token": 10000.0},
        )
        restore = Hook("restore", (sys.executable, "-c", "pass"), 30.0)
        config = ControlConfig(
            FLEET,
            {"restore": restore},
            runs_dir=self.runs,
            router=RouterEndpoint(ROUTER),
            load=load,
        )
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "NARWHAL_CONTROL_TOKEN": TOKEN,
            "FAKE_AIPERF_STATE": str(self.state),
            "FAKE_AIPERF_MODE": self.mode,
            "FAKE_AIPERF_SLEEP": str(self.sleep),
        }
        self.service = ControlService(config, runner=runner_for(config, self.env), env=self.env)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=create_app(self.service, TOKEN, [workload_routes(load)])
            ),
            base_url="http://control",
            headers={"authorization": f"Bearer {TOKEN}"},
        )
        self.addAsyncCleanup(self.client.aclose)
        self.addAsyncCleanup(self.service.close)

    def executable(self) -> str:
        return str(self.aiperf)

    async def start_session(self) -> Path:
        response = await self.client.post("/api/session")
        self.assertEqual(response.status_code, 201, response.text)
        return self.runs / "sessions" / response.json()["result"]["session"]

    async def start_job(self, **params: Any) -> dict[str, Any]:
        response = await self.client.post("/api/jobs", json=params)
        self.assertEqual(response.status_code, 201, response.text)
        return dict(response.json()["result"]["job"])

    async def finished_job(self) -> dict[str, Any]:
        assert self.service.jobs is not None
        for _ in range(500):
            if not self.service.jobs.busy:
                break
            await asyncio.sleep(0.02)
        else:
            self.fail("the load job did not finish")
        response = await self.client.get("/api/jobs/current")
        return dict(response.json())

    async def written(self, path: Path) -> Any:
        for _ in range(500):
            if path.exists():
                try:
                    return json.loads(path.read_text())
                except ValueError:
                    pass
            await asyncio.sleep(0.02)
        self.fail(f"{path.name} was not written")

    def record(self, session: Path) -> dict[str, Any]:
        return dict(json.loads((session / "run.json").read_text()))


class CompletedJobTests(RunnerCase):
    async def test_a_job_records_its_parameters_and_client_results(self) -> None:
        session = await self.start_session()
        job = await self.start_job(workload="chat-512-256", rate=2, duration_s=30)
        self.assertEqual(job["state"], "running")
        job = await self.finished_job()
        self.assertEqual(job["state"], "succeeded", job)
        self.assertIsNone(job["error"])
        params = {"workload": "chat-512-256", "kind": "synthetic", "rate": 2, "duration_s": 30}
        self.assertEqual(job["params"], params)
        result = job["result"]
        directory = session / "jobs/job-001"
        argv = await self.written(self.state / "argv.json")
        self.assertEqual(result["command"], [str(self.aiperf), *argv])
        self.assertEqual(
            argv[argv.index("--artifact-dir") + 1], str(directory.resolve() / "aiperf")
        )
        self.assertEqual(argv[argv.index("--request-rate") + 1], "2")
        self.assertEqual(argv[argv.index("--url") + 1], ROUTER)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["workload"], SYNTHETIC.document())
        self.assertEqual(result["requests"], EXPECTED_REQUESTS)
        self.assertEqual(result["throughput"]["requests_per_s"], 9.7)
        self.assertEqual(result["goodput"]["good_requests"], 91)
        self.assertEqual(result["latency"]["ttft"]["p99"], 250.0)
        self.assertEqual(result["summary"], "aiperf/profile_export_aiperf.json")
        self.assertNotIn("tail", result)
        log = directory / result["log"]
        self.assertIn("aiperf starting", log.read_text())
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((directory / "aiperf").stat().st_mode), 0o700)
        self.assertNotIn("NARWHAL_CONTROL_TOKEN", await self.written(self.state / "env.json"))
        entries = self.record(session)["actions"]
        self.assertEqual(
            [(entry["action"], entry["outcome"]) for entry in entries],
            [("session.start", "ok"), ("job.start", "ok"), ("job.complete", "ok")],
        )
        self.assertEqual(
            entries[1]["params"], {"workload": "chat-512-256", "rate": 2, "duration_s": 30}
        )
        self.assertEqual(entries[2]["result"]["params"], params)
        self.assertEqual(entries[2]["result"]["result"], result)

    async def test_a_trace_job_records_the_trace_digest(self) -> None:
        await self.start_session()
        await self.start_job(workload="replay", concurrency=4)
        job = await self.finished_job()
        self.assertEqual(job["state"], "succeeded", job)
        digest = hashlib.sha256(self.trace.read_bytes()).hexdigest()
        self.assertEqual(job["result"]["workload"]["sha256"], digest)
        argv = await self.written(self.state / "argv.json")
        self.assertIn("--fixed-schedule", argv)
        self.assertEqual(argv[argv.index("--input-file") + 1], str(self.trace))
        self.assertEqual(argv[argv.index("--isl-block-size") + 1], "300")
        self.assertNotIn("--request-rate", argv)

    async def test_invalid_parameters_are_refused_before_aiperf_starts(self) -> None:
        await self.start_session()
        response = await self.client.post("/api/jobs", json={"workload": "replay", "rate": 2})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["detail"],
            "timestamped_trace workload 'replay' replays its recorded timestamps and takes no rate",
        )
        self.assertFalse((self.state / "argv.json").exists())

    async def test_the_workload_library_is_listed_without_trace_paths(self) -> None:
        response = await self.client.get("/api/workloads")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["workloads"],
            [
                {
                    "name": "chat-512-256",
                    "kind": "synthetic",
                    "isl": 512,
                    "isl_stddev": 0.0,
                    "osl": 256,
                    "osl_stddev": 16.0,
                },
                {"name": "replay", "kind": "timestamped_trace", "block_size": 300},
            ],
        )


class SlowJobTests(RunnerCase):
    sleep = 1.0

    async def test_a_second_job_is_refused_while_aiperf_runs(self) -> None:
        session = await self.start_session()
        await self.start_job(workload="chat-512-256", concurrency=2, duration_s=5)
        second = await self.client.post(
            "/api/jobs", json={"workload": "chat-512-256", "rate": 1, "duration_s": 5}
        )
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["detail"], "load job job-001 is running")
        self.assertEqual((await self.finished_job())["state"], "succeeded")
        await self.start_job(workload="chat-512-256", rate=1, duration_s=5)
        self.assertEqual((await self.finished_job())["id"], "job-002")
        self.assertEqual(
            [(entry["action"], entry["outcome"]) for entry in self.record(session)["actions"]],
            [
                ("session.start", "ok"),
                ("job.start", "ok"),
                ("job.start", "refused"),
                ("job.complete", "ok"),
                ("job.start", "ok"),
                ("job.complete", "ok"),
            ],
        )


class StoppedJobTests(RunnerCase):
    mode = "hang"

    async def test_stopping_a_job_kills_the_aiperf_process_group(self) -> None:
        session = await self.start_session()
        await self.start_job(workload="chat-512-256", rate=1, duration_s=600)
        pids = await self.written(self.state / "pids.json")
        self.assertTrue(all(alive(pid) for pid in pids))
        response = await self.client.post("/api/jobs/current/stop")
        self.assertEqual(response.status_code, 200, response.text)
        job = response.json()["result"]["job"]
        self.assertEqual(job["state"], "stopped")
        self.assertEqual(job["result"]["exit_code"], -signal.SIGTERM)
        self.assertEqual(job["result"]["pid"], pids[0])
        self.assertIn("no profile_export_aiperf.json", job["result"]["summary_error"])
        for _ in range(250):
            if not any(alive(pid) for pid in pids):
                break
            await asyncio.sleep(0.02)
        self.assertFalse([pid for pid in pids if alive(pid)], "AIPerf processes survived the stop")
        self.assertFalse(self.service.jobs is None or self.service.jobs.busy)
        self.assertEqual(
            [(entry["action"], entry["outcome"]) for entry in self.record(session)["actions"]][-2:],
            [("job.complete", "ok"), ("job.stop", "ok")],
        )

    async def test_ending_the_session_stops_aiperf(self) -> None:
        await self.start_session()
        await self.start_job(workload="replay")
        pids = await self.written(self.state / "pids.json")
        response = await self.client.post("/api/session/end")
        self.assertEqual(response.status_code, 200, response.text)
        for _ in range(250):
            if not any(alive(pid) for pid in pids):
                break
            await asyncio.sleep(0.02)
        self.assertFalse([pid for pid in pids if alive(pid)])


class FailedJobTests(RunnerCase):
    mode = "fail"

    async def test_a_failed_aiperf_exit_fails_the_job(self) -> None:
        session = await self.start_session()
        await self.start_job(workload="chat-512-256", rate=1, duration_s=10)
        job = await self.finished_job()
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["error"], "AIPerfFailed: aiperf exited 3")
        self.assertEqual(job["result"]["exit_code"], 3)
        self.assertIn("tokenizer not found", job["result"]["tail"])
        complete = self.record(session)["actions"][-1]
        self.assertEqual((complete["action"], complete["outcome"]), ("job.complete", "failed"))
        self.assertEqual(complete["error"], "AIPerfFailed: aiperf exited 3")


class MissingSummaryTests(RunnerCase):
    mode = "no-summary"

    async def test_a_clean_exit_without_a_summary_fails_the_job(self) -> None:
        await self.start_session()
        await self.start_job(workload="chat-512-256", rate=1, duration_s=10)
        job = await self.finished_job()
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["result"]["exit_code"], 0)
        self.assertIn("aiperf wrote no readable summary", job["error"])


class MissingExecutableTests(RunnerCase):
    def executable(self) -> str:
        return str(self.state / "no-such-aiperf")

    async def test_a_missing_executable_fails_the_job(self) -> None:
        await self.start_session()
        await self.start_job(workload="chat-512-256", rate=1, duration_s=10)
        job = await self.finished_job()
        self.assertEqual(job["state"], "failed")
        self.assertTrue(job["error"].startswith("AIPerfFailed: aiperf could not start"), job)
        self.assertIsNone(job["result"]["exit_code"])


class CliWiringTests(unittest.TestCase):
    def test_cli_serves_load_jobs_when_configured(self) -> None:
        load = json.loads((ROOT / "config/fleet-control.example.json").read_text())["load"]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "control.json"
            document = {"fleet": str(FLEET), "hooks": {"restore": {"argv": ["x"]}}, "load": load}
            path.write_text(json.dumps(document))
            with (
                mock.patch.dict(control_cli.os.environ, {"NARWHAL_CONTROL_TOKEN": TOKEN}),
                mock.patch.object(control_cli, "check_http_bind"),
                mock.patch.object(control_cli.uvicorn, "run") as run,
            ):
                self.assertEqual(control_cli.main(["--config", str(path)]), 0)
        service = run.call_args.args[0].state.service
        self.assertIsNotNone(service.jobs)
        runner = service.jobs.runner
        self.assertIsInstance(runner, AIPerfRunner)
        self.assertEqual(runner.url, service.config.router.url)
        self.assertEqual(sorted(runner.load.workloads), sorted(load["workloads"]))
        self.assertNotIn("NARWHAL_CONTROL_TOKEN", runner._env)


if __name__ == "__main__":
    unittest.main()
