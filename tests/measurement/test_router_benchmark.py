"""Router benchmark helpers, report rows and the simulated-engine router contract."""

import copy
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from narwhal.config import FleetConfig
from narwhal.engines.attestation import EngineIdentity
from narwhal.profiling.generation import (
    generation_problem,
    identity_generation,
    read_generation,
)
from narwhal.profiling.store import ProfileStore
from narwhal.serving.app import create_app
from narwhal.serving.router.routing import NarwhalRouter
from narwhal.types import Role
from tests.wire import EngineWire
from tools.measurement import load_trial as trial
from tools.measurement.router_benchmark import cli as bench_cli
from tools.measurement.router_benchmark import client as bench_client
from tools.measurement.router_benchmark import comparison as bench_comparison
from tools.measurement.router_benchmark import cpus as bench_cpus
from tools.measurement.router_benchmark import fleet as bench_fleet
from tools.measurement.router_benchmark import processes as bench_processes
from tools.measurement.router_benchmark import render as bench_render
from tools.measurement.router_benchmark import report as bench_report
from tools.measurement.router_benchmark import run as bench_run
from tools.measurement.router_benchmark import sampling as bench_sampling
from tools.measurement.simulated_engine import SIMULATED_VERSION, SimulatedEngine

BUSY = "narwhal_event_loop_busy_seconds_total"
ALLOCATION = bench_cpus.Allocation(
    router=0, router_siblings=[40], clients=[1, 2, 3], engines=[4, 5, 6], driver=7
)
ROLES = {"e0": "prefill", "e1": "decode", "e2": "decode"}
SATURATED = json.dumps(
    {
        "error": {
            "message": "router saturated: loop lag 0.60s, request sizing 0.00s",
            "type": "server_overloaded_error",
        }
    }
)
RETENTION = json.dumps(
    {
        "error": {
            "message": "HTTP retention limit reached",
            "type": "server_overloaded_error",
        }
    }
)


def refusal(priced):
    message = (
        f"cheapest placement prices TTFT at {priced}s against the 2.00s budget; "
        "retry as the priced queue drains"
    )
    return json.dumps({"error": {"message": message, "type": "server_overloaded_error"}})


def row(outcome, scheduled, lag, elapsed, *, ttft=None, events=0, status=200, body=None):
    value = {
        "outcome": outcome,
        "status": status,
        "scheduled_mono": scheduled,
        "schedule_lag_s": lag,
        "elapsed_s": elapsed,
        "ttft_s": ttft,
        "token_events": events,
    }
    if body is not None:
        value["error_body"] = body
    return value


def state(rejected, refused, *, ejected=(), quarantined=(), probation=(), degraded=False):
    return {
        "admission": {"rejected": rejected, "refused": refused},
        "ejected": list(ejected),
        "quarantined": list(quarantined),
        "probation": list(probation),
        "monitoring": {"degraded": degraded},
    }


def ticks(busy, irq, total):
    return {"busy": busy, "irq": irq, "total": total}


def rows_and_samples():
    rows = [
        row("completed", 100.0, 0.01, 2.0, ttft=0.5, events=16),
        row("completed", 100.5, 0.07, 2.5, ttft=0.4, events=16),
        row("http_error", 101.0, 0.0, 0.01, status=429, body=SATURATED),
        row("http_error", 101.5, 0.0, 0.02, status=429, body=RETENTION),
        row("http_error", 102.0, 0.0, 0.03, status=503, body="upstream failure"),
    ]
    samples = {
        "before": {"mono": 99.0, "router_cpu_s": 10.0, "state": state(3, 1)},
        "t0": {
            "mono": 100.0,
            "router_cpu_s": 10.5,
            "clients": {"0": 1.0, "1": 2.0, "2": 3.0},
            "engines": {"e0": 1.0, "e1": 1.0, "e2": 1.0},
            "cores": {"0": ticks(1000, 10, 2000), "40": ticks(500, 0, 2000)},
            "router_metrics": {BUSY: 4.0},
            "late_ticks": {"e0": 0, "e1": 3, "e2": 0},
        },
        "t1": {
            "mono": 110.0,
            "router_cpu_s": 18.5,
            "clients": {"0": 10.7, "1": 11.4, "2": 4.0},
            "engines": {"e0": 10.9, "e1": 2.0, "e2": 1.5},
            "cores": {"0": ticks(1900, 60, 3000), "40": ticks(800, 100, 3000)},
            "router_metrics": {BUSY: 11.0},
            "late_ticks": {"e0": 0, "e1": 5, "e2": 0},
        },
        "after": {
            "mono": 112.0,
            "router_cpu_s": 20.0,
            "state": state(5, 2, ejected=["e2"], probation=["e1"], degraded=True),
        },
        "drain": "timeout",
    }
    return rows, samples


class CpuTests(unittest.TestCase):
    def test_proc_cpu_seconds_reads_after_the_last_parenthesis(self):
        stat = "4242 ((a b) (c)) S 1 2 3 4 5 6 7 8 9 10 250 50 1000 1000 20 0 1 0 100 4096 12\n"
        self.assertAlmostEqual(bench_cpus.proc_cpu_seconds(stat), 300 / os.sysconf("SC_CLK_TCK"))

    def test_cpu_times_selects_one_cpu_line(self):
        stat = (
            "cpu  900 9 300 9000 90 30 30 0 0 0\n"
            "cpu1 20 2 6 200 1 1 1 3 7 4\n"
            "cpu10 99 99 99 99 99 99 99 99 0 0\n"
            "intr 12345\n"
        )
        self.assertEqual(bench_cpus.cpu_times(stat, 1), {"busy": 28, "irq": 2, "total": 234})
        self.assertEqual(bench_cpus.cpu_times(stat, 10)["busy"], 297)
        with self.assertRaisesRegex(ValueError, "cpu2"):
            bench_cpus.cpu_times(stat, 2)

    def test_parse_cpu_list_expands_ranges(self):
        self.assertEqual(bench_cpus.parse_cpu_list("0-3,10"), [0, 1, 2, 3, 10])
        self.assertEqual(bench_cpus.parse_cpu_list("4,36\n"), [4, 36])
        with self.assertRaisesRegex(ValueError, "once"):
            bench_cpus.parse_cpu_list("1-3,2")

    def test_allocate_keeps_router_siblings_idle(self):
        allocation = bench_cpus.allocate(bench_cpus.parse_cpu_list("0-7"), [0, 1, 40], 2, 3)
        self.assertEqual(
            allocation,
            bench_cpus.Allocation(
                router=0,
                router_siblings=[1, 40],
                clients=[2, 3],
                engines=[4, 5, 6],
                driver=7,
            ),
        )
        with self.assertRaisesRegex(ValueError, "holds 7 CPUs; the run needs 8"):
            bench_cpus.allocate(list(range(7)), [0, 1], 2, 3)


class ProcessTests(unittest.TestCase):
    def test_child_env_carries_only_path_home_lang_and_pythonpath(self):
        with patch.dict(
            os.environ,
            {
                "PATH": "/usr/bin",
                "HOME": "/home/bench",
                "http_proxy": "http://proxy:3128",
                "HTTPS_PROXY": "http://proxy:3128",
                "NARWHAL_FLEET": "fleet.json",
            },
        ):
            self.assertEqual(
                bench_processes.child_env("/trees/base/src"),
                {
                    "PATH": "/usr/bin",
                    "HOME": "/home/bench",
                    "LANG": "C.UTF-8",
                    "PYTHONPATH": "/trees/base/src",
                },
            )
            self.assertEqual(
                bench_processes.child_env(None),
                {"PATH": "/usr/bin", "HOME": "/home/bench", "LANG": "C.UTF-8"},
            )

    def test_client_module_runs_from_another_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "tools.measurement.router_benchmark.cli",
                    "client",
                    "-h",
                ],
                cwd=directory,
                env=bench_processes.child_env(str(bench_processes.ROOT)),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--requests", result.stdout)


class SamplingTests(unittest.TestCase):
    def test_late_ticks_reads_the_simulated_engine_counter(self):
        text = SimulatedEngine("e0").metrics().decode()
        self.assertEqual(bench_sampling.late_ticks(text), 0)
        with self.assertRaisesRegex(ValueError, "simulated_engine_late_ticks_total"):
            bench_sampling.late_ticks("process_start_time_seconds 1.0\n")


class OptionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.argv = [
            "run",
            "--router-src",
            directory.name,
            "--out",
            str(Path(directory.name) / "out"),
            "--cpus",
            "0-31",
            "--rates",
            "10,20",
        ]

    def rejects(self, extra, message):
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            bench_cli.main([*self.argv, *extra])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn(message, stderr.getvalue())

    def test_defaults_pass(self):
        parser = bench_cli.parser_for()
        args = parser.parse_args(self.argv)
        bench_cli.check(parser, args)
        self.assertEqual((args.engines, args.prefill_engines, args.clients), (8, 2, 8))
        self.assertEqual(args.rates, [10.0, 20.0])

    def test_tpot_must_exceed_correction_times_interval(self):
        self.rejects(
            ["--token-interval", "0.05", "--tpot-slo", "0.1"],
            "--tpot-slo must exceed 2",
        )

    def test_write_period_must_stay_below_first_token_timeout(self):
        self.rejects(
            [
                "--frames-per-write",
                "50",
                "--token-interval",
                "0.05",
                "--tpot-slo",
                "1.0",
            ],
            "first-token timeout",
        )

    def test_duration_must_exceed_ramp(self):
        self.rejects(["--duration", "5.12"], "--duration must exceed")

    def test_rates_must_ascend(self):
        self.rejects(["--rates", "20,10"], "--rates must be ascending")

    def test_prefill_engines_must_leave_a_decode_engine(self):
        self.rejects(["--engines", "2", "--prefill-engines", "2"], "--prefill-engines")


class BodyTests(unittest.TestCase):
    def test_text_prompt_maps_ids_to_letters(self):
        self.assertEqual(bench_client.text_prompt([0, 25, 1]), "azb")

    def test_client_bodies_take_every_kth_sequence(self):
        offers = bench_client.client_bodies(32, 16, 10, 3, 1)
        self.assertEqual([sequence for sequence, _ in offers], [1, 4, 7])
        for _, body in offers:
            self.assertEqual(len(body["prompt"]), 32)
            self.assertTrue(set(body["prompt"]) <= set("abcdefghijklmnopqrstuvwxyz"))
            self.assertEqual(body["model"], "simulated")
            self.assertIs(body["stream"], True)
            self.assertIs(body["stream_options"]["include_usage"], True)
            self.assertIs(body["return_token_ids"], True)
            self.assertEqual(body["stream_interval"], 1)
            self.assertEqual(body["max_tokens"], 16)
            self.assertEqual(body["min_tokens"], 16)
        self.assertNotEqual(offers[0][1]["prompt"], offers[1][1]["prompt"])
        self.assertEqual(offers, bench_client.client_bodies(32, 16, 10, 3, 1))


class FleetTests(unittest.TestCase):
    def test_write_fleet_loads_with_matching_profiles(self):
        shape = bench_fleet.Shape(3, 1, 512, 256, 0.02, 1, 0.005, 2.0, 0.1)
        lines = [
            {
                "iid": f"e{index}",
                "url": f"http://127.0.0.1:{9000 + index}",
                "pid": 100 + index,
                "version": SIMULATED_VERSION,
                "process_start_time_seconds": 1700000000.25 + index,
            }
            for index in range(3)
        ]
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            cfg = FleetConfig.load(bench_fleet.write_fleet(out, lines, shape))
            self.assertEqual(
                [(spec.iid, spec.url, spec.role) for spec in cfg.engines],
                [
                    ("e0", "http://127.0.0.1:9000", Role.PREFILL),
                    ("e1", "http://127.0.0.1:9001", Role.DECODE),
                    ("e2", "http://127.0.0.1:9002", Role.DECODE),
                ],
            )
            self.assertEqual(cfg.model, "simulated")
            self.assertIs(cfg.advisory, True)
            self.assertEqual(cfg.max_connections, 4096)
            self.assertEqual((cfg.slo.ttft_s, cfg.slo.tpot_s), (2.0, 0.1))
            self.assertEqual(cfg.profiles_path, out / "profiles.json")
            self.assertEqual(cfg.state_path, out / "state.json")
            store = ProfileStore(out / "profiles.json")
            self.assertEqual(len(store), 3)
            for line in lines:
                (profile,) = store.profiles_for_engine(line["iid"])
                self.assertEqual(
                    (profile.ttft_a, profile.ttft_b, profile.ttft_c), (0.0, 0.0, 0.005)
                )
                self.assertEqual(
                    (
                        profile.tpot_slope,
                        profile.tpot_intercept,
                        profile.tpot_request_slope,
                    ),
                    (0.0, 0.02, 0.0),
                )
                self.assertEqual(
                    (profile.decode_min_requests, profile.decode_max_requests),
                    (1, 4096),
                )
                self.assertEqual(
                    (profile.decode_min_kv_tokens, profile.decode_max_kv_tokens),
                    (1, 4096 * 768),
                )
                self.assertEqual(profile.kv_capacity_tokens, 4096 * 768)
                self.assertEqual((profile.decode_fit_mape, profile.decode_cv_mape), (0.0, 0.0))
                identity = EngineIdentity("simulated", line["process_start_time_seconds"])
                self.assertEqual(profile.generation_digest, identity_generation(identity).digest)


class RunTests(unittest.TestCase):
    def test_package_digest_hashes_the_sha256sum_listing_of_py_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "b.py").write_bytes(b"b = 2\n")
            (root / "a.py").write_bytes(b"a = 1\n")
            (root / "notes.txt").write_bytes(b"notes\n")
            (root / "sub").mkdir()
            (root / "sub" / "c.py").write_bytes(b"c = 3\n")
            listing = "".join(
                f"{hashlib.sha256(data).hexdigest()}  {name}\n"
                for name, data in (("a.py", b"a = 1\n"), ("b.py", b"b = 2\n"))
            )
            digest = bench_run.package_digest(root)
            self.assertEqual(digest, hashlib.sha256(listing.encode()).hexdigest())
            (root / "a.py").write_bytes(b"a = 4\n")
            self.assertNotEqual(bench_run.package_digest(root), digest)


class SweepStopTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_failed_router_sample_ends_the_sweep_with_a_report(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        out = Path(directory.name) / "run"
        src = bench_processes.ROOT / "src"
        args = bench_cli.parser_for().parse_args(
            [
                *("run", "--router-src", str(src), "--cpus", "0-31", "--rates", "5,10"),
                *("--clients", "1", "--out", str(out)),
            ]
        )
        rows, samples = rows_and_samples()
        clean = bench_report.rate_row(5.0, 5, rows, samples, ALLOCATION, ROLES) | {
            "offered_rps": 5.0,
            "saturation_rejections": 0,
            "drain": "idle",
        }
        clean["fleet"] |= {"ejected": [], "quarantined": []}

        async def offer(self, http, rate, duration, path, *, sampled):
            path.mkdir(parents=True)
            (path / "client-0.jsonl").write_text("")
            if rate == 10.0:
                raise httpx.ReadTimeout("")
            return {"requests": 5, "client_exit": {"0": 0}, "drain": "idle"}

        imported = json.dumps(str(src / "narwhal" / "__init__.py"))
        run = bench_run.Run(args, src, "branch", out, ALLOCATION)
        with (
            patch.object(
                bench_run.subprocess, "run", return_value=SimpleNamespace(stdout=imported)
            ),
            patch.object(bench_run.Run, "start_engines", new=AsyncMock()),
            patch.object(bench_run.Run, "start_router", new=AsyncMock(return_value="sha")),
            patch.object(bench_run.Run, "offer", new=offer),
            patch.object(bench_run, "write_fleet"),
            patch.object(bench_run, "rate_row", return_value=clean),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(await run.execute(), 0)
        report = json.loads((out / "report.json").read_text())
        self.assertEqual(
            (report["stopped_by"], report["stop_detail"]), ("sample_failed", "ReadTimeout")
        )
        self.assertEqual([row["offered_rps"] for row in report["rates"]], [5.0])
        self.assertEqual(report["point"]["offered_rps"], 5.0)


class ReportTests(unittest.TestCase):
    def test_rate_row_reports_every_field(self):
        rows, samples = rows_and_samples()
        result = bench_report.rate_row(10.0, 5, rows, samples, ALLOCATION, ROLES)
        span = 103.07 - 100.01
        expected = {
            "offered_rps": 10.0,
            "offered": 5,
            "completed": 2,
            "outcomes": {"completed": 2, "http_error": 3},
            "relayed_frames": 32,
            "span_s": span,
            "relayed_frames_per_s": 32 / span,
            "requests_per_s": 2 / span,
            "router_cpu_s": 10.0,
            "router_cpu_s_per_request": 5.0,
            "relayed_frames_per_router_cpu_s": 3.2,
            "window_s": 10.0,
            "router_cpu_share": 0.8,
            "event_loop_busy_share": 0.7,
            "saturation_rejections": 2,
            "refused": 1,
            "mean_resident_decode": 3.6 / span,
            "max_schedule_lag_s": 0.07,
            "client_cpu_s": 20.1,
            "engine_cpu_s": 11.4,
        }
        self.assertEqual(
            set(result),
            set(expected)
            | {
                "router_core",
                "rejections_by_reason",
                "clients",
                "engines",
                "full_cpu",
                "fleet",
                "drain",
                "marks",
            },
        )
        for key, value in expected.items():
            if isinstance(value, float):
                self.assertAlmostEqual(result[key], value, msg=key)
            else:
                self.assertEqual(result[key], value, key)
        core = result["router_core"]
        self.assertAlmostEqual(core["busy_share"], 0.9)
        self.assertAlmostEqual(core["irq_share"], 0.05)
        self.assertAlmostEqual(core["foreign_share"], 0.15)
        self.assertAlmostEqual(core["sibling_busy_share"], 0.4)
        self.assertEqual(
            result["rejections_by_reason"],
            [
                {"status": 429, "reason": "HTTP retention limit reached", "count": 1},
                {"status": 429, "reason": "router saturated", "count": 1},
                {"status": 503, "reason": "unparsed", "count": 1},
            ],
        )
        self.assertEqual([client["cpu"] for client in result["clients"]], [1, 2, 3])
        self.assertAlmostEqual(result["clients"][0]["cpu_share"], 0.97)
        self.assertAlmostEqual(result["clients"][1]["cpu_s"], 9.4)
        self.assertEqual(
            [(e["iid"], e["role"], e["cpu"], e["late_ticks"]) for e in result["engines"]],
            [("e0", "prefill", 4, 0), ("e1", "decode", 5, 2), ("e2", "decode", 6, 0)],
        )
        self.assertAlmostEqual(result["engines"][0]["cpu_share"], 0.99)
        self.assertEqual(result["full_cpu"], ["client-0", "e0"])
        self.assertEqual(
            result["fleet"],
            {
                "ejected": ["e2"],
                "quarantined": [],
                "probation": ["e1"],
                "degraded": True,
            },
        )
        self.assertEqual(result["drain"], "timeout")
        self.assertEqual(
            result["marks"],
            [
                "client_lag",
                "degraded",
                "drain_timeout",
                "ejected",
                "engine_late",
                "foreign_cpu",
                "full_cpu",
                "incomplete",
                "probation",
                "refused",
            ],
        )

    def test_refusals_differing_in_priced_ttft_share_one_reason(self):
        rows, samples = rows_and_samples()
        rows += [
            row("http_error", 103.0, 0.0, 0.01, status=429, body=refusal("2.04")),
            row("http_error", 103.5, 0.0, 0.01, status=429, body=refusal("2.05")),
        ]
        result = bench_report.rate_row(10.0, 7, rows, samples, ALLOCATION, ROLES)
        self.assertEqual(
            result["rejections_by_reason"],
            [
                {"status": 429, "reason": "HTTP retention limit reached", "count": 1},
                {
                    "status": 429,
                    "reason": "cheapest placement prices TTFT at",
                    "count": 2,
                },
                {"status": 429, "reason": "router saturated", "count": 1},
                {"status": 503, "reason": "unparsed", "count": 1},
            ],
        )

    def test_clean_rate_has_no_marks_and_429_is_complete(self):
        rows, samples = rows_and_samples()
        rows = rows[:4]
        rows[1]["schedule_lag_s"] = 0.01
        samples["t1"]["clients"] = {"0": 2.0, "1": 3.0, "2": 4.0}
        samples["t1"]["engines"] = {"e0": 2.0, "e1": 2.0, "e2": 2.0}
        samples["t1"]["late_ticks"] = samples["t0"]["late_ticks"]
        samples["t1"]["cores"]["0"] = ticks(1770, 60, 3000)
        samples["after"]["state"] = state(5, 1)
        samples["drain"] = "idle"
        result = bench_report.rate_row(10.0, 4, rows, samples, ALLOCATION, ROLES)
        self.assertEqual(result["marks"], [])
        self.assertEqual(result["full_cpu"], [])
        result = bench_report.rate_row(10.0, 5, rows, samples, ALLOCATION, ROLES)
        self.assertEqual(result["marks"], ["incomplete"])

    def test_busy_share_is_null_without_the_counter(self):
        rows, samples = rows_and_samples()
        samples["t0"]["router_metrics"] = {}
        self.assertIsNone(
            bench_report.rate_row(10.0, 5, rows, samples, ALLOCATION, ROLES)[
                "event_loop_busy_share"
            ]
        )
        rows, samples = rows_and_samples()
        allocation = bench_cpus.Allocation(0, [], [1, 2, 3], [4, 5, 6], 7)
        result = bench_report.rate_row(10.0, 5, rows, samples, allocation, ROLES)
        self.assertIsNone(result["router_core"]["sibling_busy_share"])

    def test_select_point_takes_the_highest_rate_without_rejections(self):
        rows, samples = rows_and_samples()
        base = bench_report.rate_row(10.0, 5, rows, samples, ALLOCATION, ROLES)
        clean = [
            base | {"offered_rps": 10.0, "saturation_rejections": 0},
            base | {"offered_rps": 20.0, "saturation_rejections": 0},
            base | {"offered_rps": 30.0, "saturation_rejections": 4},
        ]
        point = bench_report.select_point(clean)
        self.assertEqual(set(point), set(bench_report.POINT_FIELDS))
        self.assertEqual(point["offered_rps"], 20.0)
        self.assertEqual(point["marks"], base["marks"])
        self.assertIsNone(bench_report.select_point(clean[2:]))

    def test_stop_reason_orders_the_stop_conditions(self):
        rows, samples = rows_and_samples()
        row_value = bench_report.rate_row(10.0, 5, rows, samples, ALLOCATION, ROLES)
        self.assertEqual(bench_report.stop_reason(row_value, True), "client_failed")
        self.assertEqual(bench_report.stop_reason(row_value, False), "saturation")
        row_value |= {"saturation_rejections": 0}
        self.assertEqual(bench_report.stop_reason(row_value, False), "ejected")
        row_value |= {"fleet": {**row_value["fleet"], "ejected": []}}
        self.assertEqual(bench_report.stop_reason(row_value, False), "drain_timeout")
        self.assertIsNone(bench_report.stop_reason(row_value | {"drain": "idle"}, False))


class ComparisonTests(unittest.TestCase):
    def runs(self, frames):
        runs = []
        for index, (label, value) in enumerate(frames):
            point = None if value is None else {"offered_rps": 10.0 * (index + 1)}
            if point is not None:
                point["relayed_frames_per_router_cpu_s"] = value
            runs.append(
                {
                    "label": label,
                    "out": f"/out/{index + 1}-{label}",
                    "router_src": f"/trees/{label}/src",
                    "router_source": f"sha-{label}",
                    "point": point,
                }
            )
        return runs

    def test_comparison_reports_medians_and_ratio(self):
        runs = self.runs(
            [
                ("base", 100.0),
                ("branch", 200.0),
                ("base", 120.0),
                ("branch", 260.0),
                ("base", 110.0),
                ("branch", 230.0),
            ]
        )
        doc = bench_comparison.comparison(runs, "base", "branch")
        self.assertEqual(doc["kind"], bench_comparison.COMPARISON_KIND)
        self.assertEqual(doc["version"], 1)
        self.assertEqual(doc["order"], ["base", "branch"] * 3)
        self.assertEqual(
            doc["runs"][0],
            {
                "label": "base",
                "out": "/out/1-base",
                "router_source": "sha-base",
                "point": runs[0]["point"],
            },
        )
        base, branch = doc["versions"]["base"], doc["versions"]["branch"]
        self.assertEqual(base["router_src"], "/trees/base/src")
        self.assertEqual(base["router_source"], "sha-base")
        self.assertEqual(base["point_rps"], [10.0, 30.0, 50.0])
        self.assertEqual(base["relayed_frames_per_router_cpu_s"], [100.0, 120.0, 110.0])
        self.assertEqual(base["median_point_rps"], 30.0)
        self.assertEqual(base["median_relayed_frames_per_router_cpu_s"], 110.0)
        self.assertEqual(branch["median_relayed_frames_per_router_cpu_s"], 230.0)
        self.assertAlmostEqual(doc["ratio"], 230.0 / 110.0)
        self.assertIn(
            "ratio branch/base frames per router CPU-s: 2.091",
            bench_render.comparison_text(doc),
        )

    def test_comparison_ratio_is_null_with_a_null_point(self):
        doc = bench_comparison.comparison(
            self.runs([("base", 100.0), ("branch", None)]), "base", "branch"
        )
        self.assertIsNone(doc["ratio"])
        self.assertIsNone(doc["versions"]["branch"]["median_point_rps"])
        self.assertEqual(doc["versions"]["base"]["median_relayed_frames_per_router_cpu_s"], 100.0)
        self.assertIn(
            "ratio branch/base frames per router CPU-s: -",
            bench_render.comparison_text(doc),
        )


class RenderTests(unittest.TestCase):
    def test_report_text_names_full_cpu_processes(self):
        rows, samples = rows_and_samples()
        result = bench_report.rate_row(10.0, 5, rows, samples, ALLOCATION, ROLES)
        clean = copy.deepcopy(result) | {"saturation_rejections": 0, "offered_rps": 5.0}
        report = {
            "label": "branch",
            "router_source": "sha-branch",
            "rates": [clean, result],
            "stopped_by": "saturation",
            "stop_detail": None,
            "point": bench_report.select_point([clean, result]),
        }
        text = bench_render.report_text(report)
        self.assertIn("full_cpu(client-0,e0)", text)
        self.assertIn("point: 5 rps, 3.2 frames/router CPU-s", text)
        self.assertIn("stopped_by: saturation\n", text)
        self.assertEqual(len(text.splitlines()), 6)
        failed = report | {"stopped_by": "sample_failed", "stop_detail": "ReadTimeout"}
        self.assertIn("stopped_by: sample_failed (ReadTimeout)\n", bench_render.report_text(failed))


class RouterContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_simulated_engines_serve_a_router_request(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        out = Path(directory.name)
        engines = {f"e{index}": SimulatedEngine(f"e{index}", prefill_s=0) for index in range(2)}
        seen = []

        async def handler(request):
            engine = engines[request.url.host]
            payload = json.loads(request.content) if request.content else {}
            seen.append((request.url.host, request.url.path, payload.get("stream")))
            reply = await engine.handle(
                request.method,
                request.url.path,
                {key.lower(): value for key, value in request.headers.items()},
                request.content,
            )
            body = reply.body
            if reply.stream is not None:
                body = b"".join(reply.stream.take(reply.stream.remaining)) + b"".join(
                    reply.stream.tail()
                )
            return httpx.Response(
                reply.status, content=body, headers={"content-type": reply.content_type}
            )

        mock = httpx.MockTransport(handler)
        lines = [
            json.loads(engine.ready_line()) | {"url": f"http://{iid}"}
            for iid, engine in engines.items()
        ]
        shape = bench_fleet.Shape(2, 1, 32, 16, 0.02, 1, 0.005, 2.0, 0.1)
        cfg = FleetConfig.load(bench_fleet.write_fleet(out, lines, shape))
        store = ProfileStore(cfg.profiles_path)
        for spec in cfg.engines:
            generation = await read_generation(spec, None, timeout_s=1, transport=mock)
            (profile,) = store.profiles_for_engine(spec.iid)
            self.assertIsNone(
                generation_problem(spec.iid, profile.generation_digest, generation.digest)
            )

        def router(*args, **kwargs):
            return NarwhalRouter(*args, transport=mock, dial=EngineWire(handler).dial, **kwargs)

        with patch("narwhal.serving.app.NarwhalRouter", side_effect=router):
            app = create_app(cfg)
        self.addAsyncCleanup(app.state.router.engines.aclose)
        app.state.router.journal.open()
        self.addCleanup(app.state.router.journal.close)
        seen.clear()
        ((_, body),) = bench_client.client_bodies(32, 16, 1, 1, 0)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router"
        ) as client:
            result = await trial.request_one(
                client, "http://router", body, "contract-0", time.monotonic(), 10
            )
        self.assertEqual(result["outcome"], "completed", result)
        self.assertEqual(result["output_tokens"], 16)
        self.assertEqual(result["token_events"], 16)
        self.assertEqual(result["input_tokens"], 32)
        self.assertIn("/tokenize", [path for _, path, _ in seen])
        self.assertIn(("e0", "/v1/completions", False), seen)
        self.assertIn(("e1", "/v1/completions", True), seen)


if __name__ == "__main__":
    unittest.main()
