"""Check role-history replay and keep overload acceptance separate from latency."""

import copy
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch

from narwhal.dev.template import reference
from tools.measurement import dev_cycle


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
                "schema_version": 1,
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


if __name__ == "__main__":
    unittest.main()
