"""Check role-history replay and keep overload acceptance separate from latency."""

import copy
import io
import json
import math
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from narwhal.dev.template import _read_template, reference
from tests.fixtures import profile
from tools.measurement import dev_cycle

FOUR_SGLANG = "four-engine-cuda-sglang-v1.json"


def small_gpu_profile(iid):
    """A profile with prefill and decode costs of the order a small GPU measures."""
    return profile(
        iid,
        ttft_a=1e-9,
        ttft_b=1.2e-5,
        ttft_c=0.01,
        tpot_slope=1e-7,
        tpot_intercept=0.008,
        tpot_request_slope=0.0005,
        decode_max_requests=2,
        decode_max_kv_tokens=20_000,
        kv_capacity_tokens=200_000,
    )


class CycleTests(unittest.TestCase):
    def setUp(self):
        self.recipe = reference()["role_cycle"]
        self.before = {
            "pools": {"prefill": ["n1", "n2"], "decode": ["n3", "n4"]},
            "flips": [],
            "control": {"advisory": False},
            "journal_run": "router-1",
            "lifecycle": {"process_starts": {"n1": 1, "n2": 2, "n3": 3, "n4": 4}},
        }
        self.after = copy.deepcopy(self.before)
        self.after["flips"] = [
            {"iid": iid, "to": role, "by": "reactive", "at": index}
            for index, (iid, role) in enumerate(
                [("n2", "decode"), ("n2", "prefill"), ("n3", "prefill"), ("n3", "decode")]
            )
        ]
        self.reports = [{"phase_pass": True}] * 3

    def assess(self):
        return dev_cycle.assess(self.before, self.after, self.reports, self.recipe)

    def test_captures_short_lived_split_between_state_snapshots(self):
        result = self.assess()
        self.assertEqual(result["observed_splits"], [[2, 2], [1, 3], [2, 2], [3, 1], [2, 2]])
        self.assertTrue(result["passed"])

    def test_rejects_missing_split_manual_move_and_process_restart(self):
        original = copy.deepcopy(self.after)
        for change in ("missing", "manual", "router", "engine"):
            with self.subTest(change=change):
                self.after = copy.deepcopy(original)
                if change == "missing":
                    self.after["flips"] = self.after["flips"][:2]
                elif change == "manual":
                    self.after["flips"][0]["by"] = "operator"
                elif change == "router":
                    self.after["journal_run"] = "router-2"
                else:
                    self.after["lifecycle"]["process_starts"]["n1"] = 42
                self.assertFalse(self.assess()["passed"])

    def test_rejects_lost_history_and_failed_steady_phase(self):
        self.before["flips"] = [{"at": -1}]
        self.assertFalse(self.assess()["passed"])
        self.before["flips"] = []
        self.reports[0] = {"phase_pass": False}
        result = self.assess()
        self.assertTrue(result["role_cycle_pass"])
        self.assertFalse(result["passed"])

    def test_burst_retains_failed_latency_and_rejects_transport_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "summary.json").write_text(
                json.dumps(
                    {
                        "client_schedule_valid": True,
                        "completed": 1,
                        "candidate_pass": False,
                    }
                )
            )
            rows = [
                {"outcome": "completed"},
                {
                    "outcome": "http_error",
                    "status": 429,
                    "error_body": "TTFT budget",
                },
            ]

            def save():
                (directory / "requests.jsonl").write_text("\n".join(map(json.dumps, rows)))

            save()
            burst = self.recipe["phases"][2]
            result = dev_cycle.phase_report(directory, burst)
            self.assertTrue(result["phase_pass"])
            self.assertFalse(result["candidate_pass"])
            self.assertFalse(
                dev_cycle.phase_report(directory, self.recipe["phases"][0])["phase_pass"]
            )
            rows[1] = {"outcome": "transport_error"}
            save()
            self.assertFalse(dev_cycle.phase_report(directory, burst)["phase_pass"])

    def test_command_saves_three_phases_and_checks_config_before_traffic(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec = reference()
            fleet = {
                "schema": "narwhal.fleet",
                "schema_version": 2,
                "model": spec["model"]["served_name"],
                "controller": copy.deepcopy(spec["controller"]),
                "slo": spec["slo"],
                "engines": [
                    {
                        "iid": f"n{i}",
                        "url": f"http://127.0.0.1:{18100 + i}",
                        "role": "prefill" if i < 3 else "decode",
                    }
                    for i in range(1, 5)
                ],
            }
            instance = {
                "schema": "narwhal.dev-instance",
                "schema_version": 1,
                "python_executable": sys.executable,
                "engine_count": 4,
                "router_url": "http://test",
            }
            for name, value in (("template", spec), ("fleet", fleet), ("instance", instance)):
                (root / f"{name}.json").write_text(json.dumps(value))
            seen = []

            async def run_trial(client, base, args):
                workload = json.loads(args.workload.read_text())
                self.assertEqual(args.run_seed, 0)
                seen.append((workload["input_tokens"], workload["seed"], args.requests))
                (args.out / "summary.json").write_text(
                    json.dumps(
                        {
                            "client_schedule_valid": True,
                            "completed": args.requests,
                            "candidate_pass": len(seen) < 3,
                        }
                    )
                )
                (args.out / "requests.jsonl").write_text('{"outcome":"completed"}\n')
                return 0 if len(seen) < 3 else 2

            with (
                redirect_stdout(io.StringIO()),
                patch.object(
                    dev_cycle.lifecycle,
                    "status",
                    return_value={"status": "ready", "run": str(root)},
                ),
                patch.object(
                    dev_cycle.trial,
                    "drain",
                    new=AsyncMock(side_effect=[self.before, self.before, self.after]),
                ),
                patch.object(dev_cycle.trial, "run_trial", side_effect=run_trial) as run,
                patch.object(dev_cycle.asyncio, "sleep", new=AsyncMock()),
            ):
                out = root / "results"
                self.assertEqual(dev_cycle.main(["--instance", str(root), "--out", str(out)]), 0)
                self.assertEqual(seen, [(256, 1731, 24), (3840, 1730, 35), (3840, 1730, 12)])
                result = json.loads((out / "summary.json").read_text())
                self.assertTrue(result["passed"])
                self.assertFalse(result["phases"][2]["candidate_pass"])
                self.assertLessEqual(result["grafana_range"]["from"], result["grafana_range"]["to"])
                self.assertEqual(dev_cycle.main(["--instance", str(root), "--dry-run"]), 0)
                run.assert_awaited()
            fleet["controller"]["reactive"]["step_s"] = 5
            (root / "fleet.json").write_text(json.dumps(fleet))
            with self.assertRaisesRegex(ValueError, "controller differs"):
                dev_cycle.check_config(root / "fleet.json", spec)


class TargetTests(unittest.TestCase):
    def setUp(self):
        self.spec = _read_template(FOUR_SGLANG)
        self.profiles = tuple(small_gpu_profile(f"n{i}") for i in range(1, 5))
        self.phases = {
            phase["name"]: dev_cycle.sized(phase, self.profiles, self.spec)
            for phase in self.spec["role_cycle"]["phases"]
        }

    def load(self, demand, engines):
        return demand / (engines * self.spec["controller"]["reactive"]["utilization"])

    def test_sglang_template_matches_the_reference_cycle_and_controller(self):
        recipe = self.spec["role_cycle"]
        self.assertEqual(self.spec["runtime"]["backend"], "sglang")
        self.assertEqual(self.spec["allocation"]["engine_count"], 4)
        self.assertIn("neighbour_prefill_rps", self.spec["profile"])
        for key in ("controller", "slo"):
            self.assertEqual(self.spec[key], reference()[key])
        self.assertEqual(recipe["expected_splits"], reference()["role_cycle"]["expected_splits"])
        self.assertEqual(
            [phase["name"] for phase in recipe["phases"]],
            [phase["name"] for phase in reference()["role_cycle"]["phases"]],
        )
        for phase in recipe["phases"]:
            dev_cycle.check_target(phase)

    def test_steady_targets_cross_expand_before_the_move_and_not_after(self):
        th = self.spec["controller"]["thresholds"]
        decode = self.phases["decode"]["sizing"]
        prefill = self.phases["prefill-steady"]["sizing"]
        self.assertAlmostEqual(decode["decode_engines"], 1.8)
        self.assertAlmostEqual(prefill["prefill_engines"], 0.9)
        # 2P:2D -> 1P:3D: decode load crosses expand at two decode engines, not at three.
        self.assertGreaterEqual(self.load(decode["decode_engines"], 2), th["expand"])
        self.assertLess(self.load(decode["decode_engines"], 3), th["expand"])
        self.assertLessEqual(self.load(decode["prefill_engines"], 1), th["shrink"])
        # 1P:3D -> 2P:2D: prefill load crosses expand at one prefill engine, not at two.
        self.assertGreaterEqual(self.load(prefill["prefill_engines"], 1), th["expand"])
        self.assertLess(self.load(prefill["prefill_engines"], 2), th["expand"])
        self.assertLessEqual(self.load(prefill["decode_engines"], 2), th["shrink"])
        # Below saturation, so the phases can meet the SLO before the move.
        self.assertLess(decode["decode_engines"] / 2, 1)
        self.assertLess(prefill["prefill_engines"] / 1, 1)

    def test_steady_phases_pass_the_demand_floor_and_arrival_guards(self):
        reactive = self.spec["controller"]["reactive"]
        slo = self.spec["slo"]
        for name in ("decode", "prefill-steady"):
            phase = self.phases[name]
            with self.subTest(name):
                sizing = phase["sizing"]
                self.assertGreater(
                    sizing["prefill_engines"] + sizing["decode_engines"], reactive["demand_floor"]
                )
                self.assertGreaterEqual(
                    phase["rate_rps"] * reactive["window_s"], reactive["min_arrivals"]
                )
                self.assertEqual(
                    phase["requests"], math.ceil(phase["rate_rps"] * phase["duration_s"])
                )
                latency = slo["ttft_s"] + phase["output_tokens"] * slo["tpot_s"]
                self.assertGreaterEqual(phase["max_inflight"], phase["rate_rps"] * latency)

    def test_burst_outlasts_the_ttft_budget_on_two_prefill_engines(self):
        burst = self.phases["prefill-burst"]
        work = burst["requests"] * burst["sizing"]["prefill_s"]
        self.assertGreaterEqual(work, burst["target"]["prefill_backlog_s"])
        self.assertGreater(work / 2, self.spec["slo"]["ttft_s"])
        self.assertEqual(burst["max_inflight"], burst["requests"])
        self.assertEqual(burst["rate_rps"], 100.0)

    def test_targets_scale_with_the_profiles(self):
        faster = tuple(
            replace(p, ttft_b=p.ttft_b / 4, tpot_intercept=p.tpot_intercept / 4)
            for p in self.profiles
        )
        for phase, key in zip(
            self.spec["role_cycle"]["phases"][:2],
            ("decode_engines", "prefill_engines"),
            strict=True,
        ):
            quick = dev_cycle.sized(phase, faster, self.spec)
            slow = self.phases[phase["name"]]
            with self.subTest(phase["name"]):
                self.assertGreater(quick["rate_rps"], slow["rate_rps"])
                self.assertAlmostEqual(quick["sizing"][key], slow["sizing"][key])

    def test_fixed_phases_stay_unchanged_without_reading_profiles(self):
        spec = reference()
        missing = Path(tempfile.gettempdir()) / "narwhal-missing-profiles.json"
        self.assertIs(dev_cycle.sized_recipe(spec, missing), spec["role_cycle"])

    def test_rejects_invalid_targets_and_light_phases(self):
        base = self.spec["role_cycle"]["phases"][0]
        for change, message in (
            ({"target": {"decode_engines": 1, "prefill_engines": 1}}, "names one of"),
            ({"target": {"engines": 1}}, "names one of"),
            ({"target": {"decode_engines": 0}}, "positive number"),
            ({"target": {"decode_engines": True}}, "positive number"),
            ({"duration_s": 0}, "positive duration_s"),
        ):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, message):
                dev_cycle.check_target({**base, **change})
        with self.assertRaisesRegex(ValueError, "requires a positive rate_rps"):
            dev_cycle.check_target({**self.spec["role_cycle"]["phases"][2], "rate_rps": 0})
        light = {**base, "target": {"decode_engines": 0.001}}
        with self.assertRaisesRegex(ValueError, "per 30 s demand window"):
            dev_cycle.sized(light, self.profiles, self.spec)
        with self.assertRaisesRegex(ValueError, "requires the run's profiles"):
            dev_cycle.sized(base, (), self.spec)

    def test_command_sizes_phases_from_the_run_profiles(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec = self.spec
            fleet = {
                "schema": "narwhal.fleet",
                "schema_version": 2,
                "model": spec["model"]["served_name"],
                "controller": copy.deepcopy(spec["controller"]),
                "slo": spec["slo"],
                "engines": [
                    {
                        "iid": f"n{i}",
                        "url": f"http://127.0.0.1:{18100 + i}",
                        "role": "prefill" if i < 3 else "decode",
                    }
                    for i in range(1, 5)
                ],
                "profiles": {"path": str(root / "profiles.json")},
            }
            instance = {
                "schema": "narwhal.dev-instance",
                "schema_version": 1,
                "python_executable": sys.executable,
                "engine_count": 4,
                "router_url": "http://test",
            }
            for name, value in (("template", spec), ("fleet", fleet), ("instance", instance)):
                (root / f"{name}.json").write_text(json.dumps(value))
            before = {
                "pools": {"prefill": ["n1", "n2"], "decode": ["n3", "n4"]},
                "flips": [],
                "control": {"advisory": False},
                "journal_run": "router-1",
                "lifecycle": {"process_starts": {"n1": 1, "n2": 2, "n3": 3, "n4": 4}},
            }
            after = copy.deepcopy(before)
            after["flips"] = [
                {"iid": iid, "to": role, "by": "reactive", "at": index}
                for index, (iid, role) in enumerate(
                    [("n2", "decode"), ("n2", "prefill"), ("n3", "prefill"), ("n3", "decode")]
                )
            ]
            offered = []

            async def run_trial(client, base, args):
                offered.append((args.rate, args.requests, args.max_inflight))
                (args.out / "summary.json").write_text(
                    json.dumps(
                        {"client_schedule_valid": True, "completed": 1, "candidate_pass": True}
                    )
                )
                (args.out / "requests.jsonl").write_text('{"outcome":"completed"}\n')

            store = Mock()
            store.return_value.all_profiles.return_value = self.profiles
            with (
                redirect_stdout(io.StringIO()),
                patch.object(dev_cycle, "ProfileStore", store),
                patch.object(
                    dev_cycle.lifecycle,
                    "status",
                    return_value={"status": "ready", "run": str(root)},
                ),
                patch.object(
                    dev_cycle.trial, "drain", new=AsyncMock(side_effect=[before, before, after])
                ),
                patch.object(dev_cycle.trial, "run_trial", side_effect=run_trial),
                patch.object(dev_cycle.asyncio, "sleep", new=AsyncMock()),
            ):
                out = root / "results"
                self.assertEqual(dev_cycle.main(["--instance", str(root), "--out", str(out)]), 0)
            store.assert_called_once_with(root / "profiles.json")
            expected = [
                tuple(self.phases[name][key] for key in ("rate_rps", "requests", "max_inflight"))
                for name in ("decode", "prefill-steady", "prefill-burst")
            ]
            self.assertEqual(offered, expected)
            saved = json.loads((out / "phases.json").read_text())
            self.assertEqual([phase["requests"] for phase in saved], [e[1] for e in expected])
            self.assertEqual(json.loads((out / "template.json").read_text()), spec)
            summary = json.loads((out / "summary.json").read_text())
            self.assertTrue(summary["passed"])
            self.assertAlmostEqual(summary["phases"][0]["sizing"]["decode_engines"], 1.8)


if __name__ == "__main__":
    unittest.main()
