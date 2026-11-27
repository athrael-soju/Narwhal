"""`narwhal deploy run --trial` against stubbed hosts, engines, load client and Prometheus."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from narwhal.deployment import deploy, run_record
from narwhal.dev import cli
from tests.deployment.deploy_hosts import Fleet

TRIAL = {
    "schema": "narwhal.capacity-trial",
    "schema_version": 1,
    "input_tokens": 1024,
    "output_tokens": 128,
    "seed": 1729,
    "requests": 4,
    "rates_rps": [0.5, 1],
    "ttft_s": 2,
    "tpot_s": 0.05,
    "attainment": 0.95,
}


class CapacityTrialTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.fleet = Fleet(self.root)
        self.run = self.fleet.run_directory
        self.trial = self.root / "trial.json"
        self.trial.write_text(json.dumps(TRIAL))
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(self.fleet.patched())
        self.env = {**self.fleet.env, "NARWHAL_PREFIX_CACHING": "false"}
        stack.enter_context(patch.dict(os.environ, self.env, clear=True))
        cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, cwd)

    def deploy(self, action: str, *args: str) -> tuple[int, dict]:
        out = io.StringIO()
        self.stderr = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(self.stderr):
            code = cli.main(["deploy", action, "--run", str(self.run), *args, "--format", "json"])
        return code, json.loads(out.getvalue())

    def attempt(self, number: int = 1) -> Path:
        return self.run / f"stages/trial/attempt-{number}"

    def client_runs(self) -> list[dict[str, str]]:
        return [
            dict(zip(argv[4::2], argv[5::2], strict=False))
            for argv in self.fleet.load_argv
            if argv[3] == "run"
        ]

    def test_run_with_a_trial_offers_each_rate_reconciles_checks_the_series_and_rings(self):
        code, result = self.deploy("run", "--trial", str(self.trial))
        self.assertEqual((code, result["status"]), (0, "success"), (result, self.stderr.getvalue()))
        self.assertIn("trial", result["data"]["ran"])
        trial = result["data"]["trial"]
        self.assertTrue(trial["passed"])
        self.assertEqual([rate["rate_rps"] for rate in trial["rates"]], [0.5, 1])
        self.assertTrue(all(rate["passed"] and rate["reconciled"] for rate in trial["rates"]))
        self.assertEqual(trial["ring"], "passed")
        self.assertEqual(
            {row["series"] for row in trial["series"]},
            {"engine", "request", "token", "role", "pool-load"},
        )
        # The stage closes its Prometheus client once the checks are done.
        self.assertTrue(all(client.is_closed for client in self.fleet.prometheus_clients))
        self.assertEqual(len(self.fleet.prometheus_clients), 1)
        # The workload is prepared once, then each rate runs in order with the trial's limits,
        # through the load balancer's forward.
        self.assertEqual([argv[3] for argv in self.fleet.load_argv], ["prepare", "run", "run"])
        prepare = dict(
            zip(self.fleet.load_argv[0][4::2], self.fleet.load_argv[0][5::2], strict=True)
        )
        self.assertEqual(
            (prepare["--input-tokens"], prepare["--output-tokens"], prepare["--seed"]),
            ("1024", "128", "1729"),
        )
        runs = self.client_runs()
        self.assertEqual([run["--rate"] for run in runs], ["0.5", "1"])
        for run in runs:
            self.assertEqual(
                (run["--requests"], run["--ttft"], run["--tpot"], run["--attainment"]),
                ("4", "2", "0.05", "0.95"),
            )
            self.assertEqual(run["--expected-model"], "served-model")
            self.assertEqual(self.fleet._forwarded(run["--base"]), ("node-1", "10.0.0.1:8080"))
        # Each router and Prometheus on the primary's host are forwarded, and every forward is
        # closed before the KV ring.
        reached = {
            (host, f"{address}:{port}")
            for host, ports in self.fleet.forwards
            for address, port in ports.values()
        }
        self.assertEqual(
            reached,
            {
                ("node-1", "10.0.0.1:8080"),
                ("node-1", "10.0.0.1:8000"),
                ("node-2", "10.0.0.2:8000"),
                ("node-1", "127.0.0.1:9090"),
            },
        )
        self.assertEqual(self.fleet.open_forwards, 0)
        self.assertEqual(self.fleet.calls["ring"], 1)
        # Every series query is scoped to the fleet.
        self.assertTrue(all('fleet="fleet-a"' in query for query in self.fleet.queries))
        attempt = self.attempt()
        for name in (
            "trial.json",
            "prefix-caching.json",
            "workload/workload.json",
            "rate-0.5/reconcile.json",
            "rate-0.5/journal-rows.json",
            "rate-0.5/samples.json",
            "rate-1/summary.json",
            "series.json",
            "host/ring.json",
            "trial-result.json",
        ):
            self.assertTrue((attempt / name).is_file(), name)
        self.assertEqual(
            json.loads((attempt / "prefix-caching.json").read_text()),
            {"engine-1": False, "engine-2": False},
        )
        reconciled = json.loads((attempt / "rate-0.5/reconcile.json").read_text())
        self.assertEqual(reconciled["diagnostics"], [])
        self.assertEqual(reconciled["journal"]["terminal"], 5)
        # The status the routers export covers the trial.
        status = json.loads((self.run / "deployment-status.json").read_text())
        self.assertIn("trial", {stage["stage"] for stage in status["stages"]})

        code, result = self.deploy("run", "--trial", str(self.trial))
        self.assertEqual((code, result["data"]["ran"]), (0, []))
        self.assertTrue(result["data"]["trial"]["passed"])

    def test_a_changed_trial_reruns_only_the_trial(self):
        self.deploy("run", "--trial", str(self.trial))
        self.trial.write_text(json.dumps({**TRIAL, "rates_rps": [2]}))
        code, result = self.deploy("run", "--trial", str(self.trial))
        self.assertEqual((code, result["data"]["ran"]), (0, ["trial"]), result)
        self.assertEqual(self.client_runs()[-1]["--rate"], "2")
        # A run without --trial stops at the monitoring stack and leaves the trial alone.
        code, result = self.deploy("run")
        self.assertEqual((code, result["data"]["ran"]), (0, []))
        self.assertNotIn("trial", result["data"])

    def test_an_engine_with_prefix_caching_on_stops_the_trial_before_any_load(self):
        self.fleet.prefix_caching = True
        code, result = self.deploy("run", "--trial", str(self.trial))
        self.assertEqual((code, result["status"]), (1, "failed_gate"), result)
        self.assertEqual(result["data"]["failed_stage"], "trial")
        [error] = result["errors"]
        self.assertIn("engine-1: checked.json shows prefix_caching true", error["message"])
        self.assertIn("engine-2: checked.json shows prefix_caching true", error["message"])
        self.assertEqual(self.fleet.load_argv, [])
        self.assertEqual(self.fleet.forwards, [])
        self.assertEqual(result["data"]["trial"]["passed"], False)
        recorded = json.loads((self.attempt() / "prefix-caching.json").read_text())
        self.assertEqual(recorded, {"engine-1": True, "engine-2": True})

    def test_a_check_that_resolved_no_cache_setting_stops_the_trial(self):
        self.fleet.prefix_caching = None
        code, result = self.deploy("run", "--trial", str(self.trial))
        self.assertEqual(code, 1, result)
        self.assertIn("checked.json shows no prefix_caching", result["errors"][0]["message"])

    def test_plan_and_run_refuse_a_trial_unless_the_inputs_turn_prefix_caching_off(self):
        for value in (None, "true"):
            # Unset for every engine, or turned on again for engine 2 alone.
            env = {k: v for k, v in self.env.items() if k != "NARWHAL_PREFIX_CACHING"}
            if value:
                env.update(NARWHAL_PREFIX_CACHING="false", NARWHAL_NODE_2_PREFIX_CACHING=value)
            with self.subTest(value=value), patch.dict(os.environ, env, clear=True):
                for action in ("plan", "run"):
                    code, result = self.deploy(action, "--trial", str(self.trial))
                    self.assertEqual((code, result["status"]), (2, "invalid_input"), result)
                    messages = [error["message"] for error in result["errors"]]
                    self.assertIn(
                        "engine-2: the capacity trial needs prefix caching off; set "
                        "NARWHAL_PREFIX_CACHING or NARWHAL_NODE_2_PREFIX_CACHING to false",
                        messages,
                    )
                    self.assertEqual(any("engine-1" in m for m in messages), value is None)
        self.assertFalse(self.run.exists())

    def test_an_invalid_trial_file_fails_plan_before_any_host(self):
        self.trial.write_text(json.dumps({**TRIAL, "rates_rps": [1, 0.5], "extra": 1}))
        code, result = self.deploy("plan", "--trial", str(self.trial))
        self.assertEqual(code, 2, result)
        [message] = [error["message"] for error in result["errors"]]
        self.assertIn("unknown fields extra", message)
        self.assertIn("rates_rps to a list of positive request rates in increasing order", message)

    def test_plan_with_a_trial_decides_the_trial_stage(self):
        code, result = self.deploy("plan", "--trial", str(self.trial))
        self.assertEqual(code, 0, result)
        self.assertEqual(result["data"]["stages"][-1]["stage"], "trial")
        self.deploy("run", "--trial", str(self.trial))
        code, result = self.deploy("plan", "--trial", str(self.trial))
        trial = result["data"]["stages"][-1]
        self.assertEqual((trial["stage"], trial["runs"]), ("trial", False))
        self.trial.write_text(json.dumps({**TRIAL, "requests": 8}))
        code, result = self.deploy("plan", "--trial", str(self.trial))
        self.assertEqual(
            [(row["stage"], row["reasons"]) for row in result["data"]["stages"] if row["runs"]],
            [("trial", ["trial changed"])],
        )

    def test_until_trial_needs_a_trial_and_the_trial_runs_on_ssh_hosts_only(self):
        code, result = self.deploy("plan", "--until", "trial")
        self.assertEqual(code, 2, result)
        self.assertIn("--until trial needs --trial", result["errors"][0]["message"])
        code, result = self.deploy("run", "--trial", str(self.trial), "--target", "kubernetes")
        self.assertEqual(code, 2, result)
        self.assertIn("SSH hosts only", result["errors"][0]["message"])

    def test_a_rate_below_its_attainment_stops_before_the_next_rate(self):
        self.fleet.client_exit["0.5"] = 2
        code, result = self.deploy("run", "--trial", str(self.trial))
        self.assertEqual((code, result["status"]), (1, "failed_gate"), result)
        self.assertIn("rate-0.5: attainment 0.5 is below 0.95", result["errors"][0]["message"])
        self.assertEqual([run["--rate"] for run in self.client_runs()], ["0.5"])
        self.assertEqual(self.fleet.calls["ring"], 0)
        [rate] = result["data"]["trial"]["rates"]
        self.assertEqual((rate["passed"], rate["reconciled"]), (False, True))
        # The forwards close when the stage fails.
        self.assertEqual(self.fleet.open_forwards, 0)

    def test_a_blocked_load_client_fails_the_stage_without_a_verdict(self):
        self.fleet.client_exit["0.5"] = 1
        code, result = self.deploy("run", "--trial", str(self.trial))
        self.assertEqual((code, result["status"]), (4, "error"), result)
        self.assertIn("rate-0.5: the load client exited 1", result["errors"][0]["message"])

    def test_offers_that_do_not_reconcile_with_the_journal_stop_the_trial(self):
        self.fleet.lost_rows = 1
        code, result = self.deploy("run", "--trial", str(self.trial))
        self.assertEqual((code, result["status"]), (1, "failed_gate"), result)
        message = result["errors"][0]["message"]
        self.assertIn("rate-0.5: the offers do not reconcile with the routers", message)
        self.assertIn("client_journal_count", message)
        reconciled = json.loads((self.attempt() / "rate-0.5/reconcile.json").read_text())
        self.assertIn("counter_mismatch", {item["kind"] for item in reconciled["diagnostics"]})
        self.assertEqual(self.fleet.calls["ring"], 0)

    def test_a_series_prometheus_does_not_show_stops_the_trial_before_the_ring(self):
        self.fleet.missing_series = {("engine", "n2"), ("pool-load", "decode")}
        code, result = self.deploy("run", "--trial", str(self.trial))
        self.assertEqual((code, result["status"]), (1, "failed_gate"), result)
        message = result["errors"][0]["message"]
        self.assertIn("engine: not shown for n2", message)
        self.assertIn("pool-load: not shown for decode", message)
        self.assertEqual(self.fleet.calls["ring"], 0)
        series = json.loads((self.attempt() / "series.json").read_text())
        self.assertEqual(
            [row["series"] for row in series if not row["passed"]], ["engine", "pool-load"]
        )

    def test_a_failed_kv_ring_fails_the_trial_gate(self):
        self.fleet.fail_stage = "trial"
        code, result = self.deploy("run", "--trial", str(self.trial))
        self.assertEqual((code, result["status"]), (1, "failed_gate"), result)
        self.assertEqual(result["data"]["failed_stage"], "trial")
        self.assertFalse(result["data"]["trial"]["passed"])
        self.assertEqual(len(self.client_runs()), 2)

    def test_a_workload_outside_the_profiled_domain_stops_the_trial_before_any_load(self):
        self.trial.write_text(json.dumps({**TRIAL, "input_tokens": 8192}))
        code, result = self.deploy("run", "--trial", str(self.trial))
        self.assertEqual(code, 1, result)
        self.assertIn(
            "n1: input_tokens 8192 is outside the profiled 256 to 2048",
            result["errors"][0]["message"],
        )
        self.assertEqual(self.fleet.load_argv, [])

    def test_a_single_router_fleet_takes_the_trial_through_its_router(self):
        env = {
            k: v
            for k, v in self.env.items()
            if k
            not in (
                "NARWHAL_STANDBY_ROUTER_SSH",
                "NARWHAL_STANDBY_ROUTER_URL",
                "NARWHAL_LOAD_BALANCER_URL",
                "NARWHAL_LEASE_PATH",
                "NARWHAL_HAPROXY_IMAGE",
            )
        }
        with patch.dict(os.environ, env, clear=True):
            code, result = self.deploy("run", "--trial", str(self.trial))
        self.assertEqual(code, 0, (result, self.stderr.getvalue()))
        for run in self.client_runs():
            self.assertEqual(self.fleet._forwarded(run["--base"]), ("node-1", "10.0.0.1:8000"))

    def test_a_failed_trial_shows_in_the_published_status(self):
        self.fleet.client_exit["0.5"] = 2
        self.deploy("run", "--trial", str(self.trial))
        status = json.loads((self.run / "deployment-status.json").read_text())
        states = {stage["stage"]: stage["state"] for stage in status["stages"]}
        self.assertEqual(states["trial"], "failed")
        self.assertIsNone(status["last_passed_at"])
        record = run_record.read(self.run)
        self.assertEqual(record["until"], deploy.TRIAL_STAGE)


if __name__ == "__main__":
    unittest.main()
