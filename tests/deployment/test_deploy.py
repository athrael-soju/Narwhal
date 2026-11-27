"""`narwhal deploy plan|run|status|down` over the whole graph, against stubbed hosts and engines."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from narwhal.deployment import deploy, run_record
from narwhal.dev import cli
from narwhal.profiling.calibration import MIN_SAMPLES_PER_GROUP
from tests.deployment.deploy_hosts import Fleet

ENGINES = ("engine-1", "engine-2")
EDGES = ("node-1-to-node-2", "node-2-to-node-1")
# The primary router's host, then the standby's.
ROUTERS = ("node-1", "node-2")
UNITS = 4


def every_stage() -> set[str]:
    keys = {"discover", "package", "cache-groups", "profile", "calibrate", "fleet-limits"}
    keys |= {"lease-check", "load-balancer", "monitoring"}
    keys |= {f"access[{host}]" for host in ("node-1", "node-2")}
    keys |= {f"install[{host}]" for host in ("node-1", "node-2")}
    for stage in (
        "engine-inspect",
        "engine-prepare",
        "engine-check",
        "engine-start",
        "engine-probe",
        "cache-capture",
        "attest-capture",
        "sidecar-start",
        "sidecar-verify",
    ):
        keys |= {f"{stage}[{engine}]" for engine in ENGINES}
    keys |= {"fabric-budget[engine-1]"} | {f"fabric-edge[{edge}]" for edge in EDGES}
    for stage in ("fleet-inventory", "finalize-fleet", "preflight", "router-start"):
        keys |= {f"{stage}[{host}]" for host in ROUTERS}
    return keys


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.fleet = Fleet(self.root)
        self.run = self.fleet.run_directory
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(self.fleet.patched())
        self.env = dict(self.fleet.env)
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

    def ran(self, result: dict) -> set[str]:
        return set(result["data"]["ran"])

    def test_run_reaches_a_ready_router_and_a_rerun_repeats_nothing(self):
        code, result = self.deploy("run")
        self.assertEqual((code, result["status"]), (0, "success"), result)
        self.assertEqual(self.ran(result), every_stage())
        for name in ("sidecar-1", "sidecar-2"):
            self.assertEqual(self.fleet.calls[f"start {name}"], 1)
        self.assertEqual(self.fleet.calls["start router"], 2)
        self.assertEqual(len(self.fleet.units), UNITS)
        self.assertTrue(self.fleet.monitoring_up)
        command = self.fleet.monitoring_command
        for origin in ("http://10.0.0.1:8200", "http://10.0.0.2:8200"):
            self.assertIn(f"--sidecar-url {origin}", command)
        self.assertIn("NARWHAL_FLEET_NAME=fleet-a", command)
        self.assertIn("--targets-url http://10.0.0.1:8080", command)
        self.assertIn("load-balancer", self.fleet.containers)
        fleet = json.loads((self.run / "stages/fleet-limits/attempt-1/fleet.json").read_text())
        self.assertEqual(fleet["slo"], {"ttft_s": 4.0, "tpot_s": 0.1, "router_overhead_s": 0.05})
        self.assertEqual(fleet["engine"]["first_token_timeout_s"], 1.75)
        self.assertTrue(
            fleet["engine"]["first_token_calibration_path"].endswith(
                "stages/fleet-limits/attempt-1/first-token-calibration.json"
            )
        )
        self.assertEqual(fleet["engine_contract"], {"engine": "contract"})
        uploaded = {
            target: json.loads(data)
            for target, data in self.fleet.uploads.items()
            if target.endswith("runs/deployment/deployment-status.json")
        }
        # One upload for each of the two router hosts.
        self.assertEqual(self.fleet.calls["upload deployment-status"], 2)
        (status,) = {json.dumps(document, sort_keys=True) for document in uploaded.values()}
        status = json.loads(status)
        self.assertEqual(status["schema"], "narwhal.deployment-status")
        self.assertEqual({stage["state"] for stage in status["stages"]}, {"passed"})
        self.assertIsNotNone(status["last_passed_at"])
        self.assertEqual(
            status["fabric"][0], {"edge": EDGES[0], "measured_gbps": 90.0, "budget_gbps": 1.0}
        )

        code, result = self.deploy("run")
        self.assertEqual((code, self.ran(result)), (0, set()))
        self.assertEqual(self.fleet.calls["package"], 1)
        self.assertEqual(self.fleet.calls["install[node-1]"], 1)

    def test_run_tells_the_operator_about_a_stale_lock(self):
        self.deploy("run")
        ended = subprocess.Popen([sys.executable, "-c", "pass"])
        ended.wait()
        record = run_record.read(self.run)
        owner = {**run_record.owner(), "pid": ended.pid}
        record["lock"] = {"owner": owner, "acquired_at": time.time()}
        run_record.write(self.run, record)
        code, result = self.deploy("run")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["data"]["stale_lock"]["pid"], ended.pid)
        self.assertIn(
            f"narwhal: deploy run: this run found a stale lock of pid {ended.pid} of",
            self.stderr.getvalue(),
        )
        code, result = self.deploy("run")
        self.assertNotIn("stale_lock", result["data"])
        self.assertNotIn("stale lock", self.stderr.getvalue())

    def test_a_stopped_engine_is_prepared_again_and_readmitted_to_the_running_router(self):
        self.deploy("run")
        del self.fleet.containers["engine-2"]
        code, result = self.deploy("run")
        self.assertEqual(code, 0, result)
        self.assertEqual(
            self.ran(result),
            {
                "engine-prepare[engine-2]",
                "engine-check[engine-2]",
                "engine-start[engine-2]",
                "engine-probe[engine-2]",
                "cache-capture[engine-2]",
                "attest-capture[engine-2]",
                "sidecar-start[engine-2]",
                "sidecar-verify[engine-2]",
                *(
                    f"{stage}[{host}]"
                    for stage in ("finalize-fleet", "preflight", "router-start")
                    for host in ROUTERS
                ),
            },
        )
        # Only the router that holds the lease readmits.
        self.assertEqual(self.fleet.readmitted, [["n2"]])
        self.assertEqual(self.fleet.calls["start router"], 2)
        self.assertEqual(self.fleet.calls["start sidecar-2"], 2)

    def test_an_engine_start_on_a_used_launch_directory_prepares_a_fresh_one_first(self):
        self.deploy("run")
        old = self.fleet.containers["engine-1"]
        # A changed check output reruns engine-start while engine-prepare holds.
        entry = run_record.read(self.run)["stages"]["engine-check[engine-1]"]
        (self.run / entry["directory"] / "checked.json").write_text('{"plan": "changed"}')
        code, result = self.deploy("run")
        self.assertEqual(code, 0, result)
        ran = self.ran(result)
        self.assertLessEqual(
            {"engine-prepare[engine-1]", "engine-check[engine-1]", "engine-start[engine-1]"},
            ran,
        )
        self.assertEqual(self.fleet.calls["engine-prepare[engine-1]"], 2)
        self.assertEqual(self.fleet.calls["engine-start[engine-1]"], 2)
        self.assertEqual(self.fleet.calls["removed unrecorded"], 0)
        self.assertNotEqual(self.fleet.containers["engine-1"], old)
        record = run_record.read(self.run)["stages"]
        self.assertEqual(record["engine-start[engine-1]"]["attempt"], 3)
        self.assertEqual(record["engine-prepare[engine-1]"]["attempt"], 2)
        code, result = self.deploy("run")
        self.assertEqual((code, self.ran(result)), (0, set()))

    def test_a_discovery_change_frees_the_engine_ports_before_inspecting_again(self):
        self.deploy("run")
        os.environ["NARWHAL_MODEL_DIR"] = "/models/other"
        code, result = self.deploy("run")
        self.assertEqual(code, 0, result)
        self.assertEqual(self.ran(result), every_stage() - {"access[node-1]", "access[node-2]"})
        self.assertEqual(len(self.fleet.units), UNITS)
        self.assertEqual(set(self.fleet.containers), {"engine-1", "engine-2", "load-balancer"})

    def test_status_records_a_restarted_sidecar_once_its_verification_passes(self):
        self.deploy("run")
        self.fleet.restart_unit("sidecar-2")
        code, result = self.deploy("status")
        self.assertEqual(code, 0, result)
        live = {check["key"]: check["state"] for check in result["data"]["live"]}
        self.assertEqual(live["sidecar_identity[engine-2]"], "restarted and checked")
        self.assertEqual(live["router_identity[node-1]"], "holds")
        self.assertEqual(len(self.fleet.verified), 1)
        entry = run_record.read(self.run)["stages"]["sidecar-start[engine-2]"]
        self.assertEqual(entry["products"]["sidecar_identity"]["restarts"], 1)

        code, result = self.deploy("run")
        self.assertEqual(
            self.ran(result),
            {"sidecar-verify[engine-2]", "finalize-fleet[node-1]", "finalize-fleet[node-2]"},
        )
        self.assertEqual(self.fleet.calls["start sidecar-2"], 1)

    def test_a_checked_restart_moves_the_process_record_down_stops(self):
        self.deploy("run")
        self.fleet.restart_unit("sidecar-2")
        self.deploy("status")
        entry = run_record.read(self.run)["stages"]["sidecar-start[engine-2]"]
        (process,) = entry["processes"]
        self.assertEqual(process, {"name": "sidecar-2", **entry["products"]["sidecar_identity"]})

    def test_a_restart_that_fails_its_check_reruns_its_stage(self):
        self.deploy("run")
        self.fleet.restart_unit("sidecar-2")
        self.fleet.fail_verify = True
        code, result = self.deploy("status")
        self.assertEqual(code, 3, result)
        live = {check["key"]: check["state"] for check in result["data"]["live"]}
        self.assertEqual(live["sidecar_identity[engine-2]"], "gone")
        code, result = self.deploy("run")
        self.assertEqual(code, 0, result)
        self.assertIn("sidecar-start[engine-2]", self.ran(result))
        self.assertEqual(self.fleet.calls["start sidecar-2"], 2)

    def test_plan_reads_a_restarted_unit_without_running_its_check(self):
        self.deploy("run")
        self.fleet.restart_unit("sidecar-2")
        code, result = self.deploy("plan")
        self.assertEqual(code, 0, result)
        self.assertEqual(self.fleet.verified, [])
        self.assertEqual(
            result["data"]["notes"],
            [
                "sidecar_identity[engine-2] restarted; run checks it first and reruns "
                "sidecar-start[engine-2] if the check fails"
            ],
        )
        planned = {row["stage"] for row in result["data"]["stages"] if row["runs"]}
        self.assertEqual(
            planned,
            {"sidecar-verify[engine-2]", "finalize-fleet[node-1]", "finalize-fleet[node-2]"},
        )

    def test_every_run_subcommand_reports_a_held_run_alike(self):
        self.deploy("run")
        self.fleet.restart_unit("sidecar-2")
        with run_record.locked(self.run):
            for action in ("run", "down"):
                with self.subTest(action=action):
                    code, result = self.deploy(action)
                    self.assertEqual((code, result["status"]), (4, "error"))
                    self.assertEqual(result["errors"][0]["code"], "operation_failed")
            code, result = self.deploy("status")
        self.assertEqual(code, 3, result)
        self.assertTrue(result["data"]["locked"])
        live = {check["key"]: check["state"] for check in result["data"]["live"]}
        self.assertEqual(live["sidecar_identity[engine-2]"], "restarted, not checked")
        self.assertEqual(self.fleet.verified, [])
        self.assertEqual(len(self.fleet.units), UNITS)

    def test_a_changed_slo_rewrites_the_limits_and_restarts_the_router(self):
        self.deploy("run")
        os.environ["NARWHAL_TTFT_S"] = "5"
        code, result = self.deploy("run")
        self.assertEqual(code, 0, result)
        self.assertEqual(self.ran(result), SLO_RERUN)
        self.assertEqual(self.fleet.calls["start router"], 4)
        self.assertEqual(len(self.fleet.units), UNITS)
        # The standby ran the old fleet, so the primary started the pair afresh.
        self.assertEqual(self.fleet.calls["remove the lease"], 2)

    def test_a_lease_path_the_hosts_do_not_share_stops_the_run_before_the_routers(self):
        self.fleet.lease_coherent = False
        code, result = self.deploy("run")
        self.assertEqual((code, result["status"]), (1, "failed_gate"), result)
        error = result["errors"][0]
        self.assertEqual(error["stage"], "lease-check")
        self.assertIn("does not read the primary's claim", error["message"])
        self.assertEqual(self.fleet.calls["start router"], 0)
        self.fleet.lease_coherent = True
        code, result = self.deploy("run")
        self.assertEqual(code, 0, result)
        self.assertIn("router-start[node-1]", self.ran(result))

    def test_a_lost_primary_rejoins_the_standby_that_took_over(self):
        self.deploy("run")
        self.assertEqual(self.fleet.calls["remove the lease"], 1)
        del self.fleet.units[
            next(n for n in self.fleet.units if n.startswith("node-1:") and "-router." in n)
        ]
        self.fleet.active_router = "node-2"
        code, result = self.deploy("run")
        self.assertEqual(code, 0, result)
        self.assertEqual(self.ran(result), {"router-start[node-1]"})
        # The standby holds the lease, so the lease stays and the primary rejoins as standby.
        self.assertEqual(self.fleet.calls["remove the lease"], 1)
        self.assertEqual(self.fleet.calls["start router"], 3)

    def test_router_overhead_above_its_input_fails_router_start(self):
        self.fleet.overhead_problem = "router overhead 0.0900s exceeds NARWHAL_ROUTER_OVERHEAD_S"
        code, result = self.deploy("run")
        self.assertEqual((code, result["status"]), (1, "failed_gate"), result)
        self.assertEqual(result["errors"][0]["stage"], "router-start[node-1]")
        self.assertIn("router overhead 0.0900s", result["errors"][0]["message"])

    def test_a_failed_stage_names_its_recovery_and_the_next_run_resumes_there(self):
        self.fleet.fail_stage = "engine-check"
        code, result = self.deploy("run")
        self.assertEqual((code, result["status"]), (1, "failed_gate"))
        error = result["errors"][0]
        self.assertEqual(error["stage"], "engine-check[engine-1]")
        # Both router hosts have the package, so each receives the failed run's status.
        self.assertEqual(self.fleet.calls["upload deployment-status"], 2)
        status = json.loads((self.run / "deployment-status.json").read_text())
        self.assertIn(
            {"stage": "engine-check", "instance": "engine-1", "state": "failed"}, status["stages"]
        )
        self.assertIsNone(status["last_passed_at"])
        self.assertEqual(
            error["context"]["recovery"],
            "https://athrael-soju.github.io/Narwhal/deploy/03-Validate-Engines/"
            "#preparing-checking-and-starting-each-engine",
        )
        self.fleet.fail_stage = None
        code, result = self.deploy("run")
        self.assertEqual(code, 0)
        self.assertNotIn("engine-prepare[engine-1]", self.ran(result))
        self.assertIn("engine-check[engine-1]", self.ran(result))
        self.assertEqual(self.fleet.calls["engine-prepare[engine-1]"], 1)

    def test_a_host_step_that_fails_without_a_verdict_is_an_operational_error(self):
        self.fleet.fail_stage = "engine-inspect"
        self.fleet.fail_status = 4
        code, result = self.deploy("run")
        self.assertEqual((code, result["status"]), (4, "error"))
        self.assertEqual(result["errors"][0]["code"], "operation_failed")
        self.assertEqual(result["data"]["failed_stage"], "engine-inspect[engine-1]")

    def test_plan_names_every_missing_input_before_any_host(self):
        del os.environ["NARWHAL_TTFT_S"]
        os.environ["NARWHAL_PROFILE_PREFILL_LENS"] = "256,x"
        code, result = self.deploy("plan")
        self.assertEqual((code, result["status"]), (2, "invalid_input"))
        messages = [error["message"] for error in result["errors"]]
        self.assertIn("set NARWHAL_TTFT_S to a finite number of at least 0", messages)
        self.assertIn(
            "set NARWHAL_PROFILE_PREFILL_LENS to comma-separated positive integers", messages
        )
        self.assertEqual(sum(self.fleet.calls.values()), 0)
        self.assertFalse(self.run.exists())

    def test_plan_decides_each_stage_without_running_one(self):
        code, result = self.deploy("plan")
        self.assertEqual(code, 0, result)
        stages = {row["stage"]: row for row in result["data"]["stages"]}
        self.assertEqual(stages["access[node-1]"]["reasons"], ["no attempt yet"])
        self.assertEqual(stages["discover"]["reasons"], ["no attempt yet"])
        self.assertEqual(stages["fabric-budget"]["reasons"], ["runs after cache-groups"])
        self.assertIn("router-start[node-1]", stages)
        self.assertEqual(sum(self.fleet.calls.values()), 0)
        self.assertFalse(self.run.exists())

        self.deploy("run")
        os.environ["NARWHAL_TTFT_S"] = "5"
        code, result = self.deploy("plan")
        stages = {row["stage"]: row for row in result["data"]["stages"]}
        self.assertFalse(stages["calibrate"]["runs"])
        self.assertEqual(stages["fleet-limits"]["reasons"], ["slo changed"])
        self.assertEqual(stages["preflight[node-1]"]["reasons"], ["slo changed"])
        self.assertEqual({name for name, row in stages.items() if row["runs"]}, SLO_RERUN)
        self.assertEqual(
            run_record.read(self.run)["inputs"]["slo"]["digest"],
            deploy.inputs({**self.env})["slo"].digest,
        )

    def test_plan_names_a_host_that_cannot_run_supervised_units(self):
        with patch.object(deploy.supervision, "check_systemd_host", lambda runner: ["no linger"]):
            code, result = self.deploy("plan")
        self.assertEqual((code, result["status"]), (1, "failed_gate"))
        self.assertIn("node-1: no linger", [error["message"] for error in result["errors"]])

    def test_down_stops_what_the_run_started(self):
        self.deploy("run")
        code, result = self.deploy("down")
        self.assertEqual(code, 0)
        names = {(p["name"], p["stopped"]) for p in result["data"]["processes"]}
        self.assertEqual(
            names,
            {
                ("monitoring", True),
                ("load-balancer", True),
                ("router", True),
                ("sidecar-1", True),
                ("sidecar-2", True),
                ("engine-1", True),
                ("engine-2", True),
            },
        )
        self.assertEqual(self.fleet.units, {})
        self.assertEqual(set(self.fleet.containers), set())
        self.assertFalse(self.fleet.monitoring_up)

    def test_down_leaves_a_unit_another_start_owns_and_stops_the_rest(self):
        self.deploy("run")
        unit = next(name for name in self.fleet.units if name.endswith("-sidecar-1.service"))
        _, invocation, restarts = self.fleet.units[unit]
        self.fleet.units[unit] = ("another-start", invocation, restarts)
        code, result = self.deploy("down")
        self.assertEqual(code, 0)
        stopped = {p["name"]: p["stopped"] for p in result["data"]["processes"]}
        self.assertFalse(stopped["sidecar-1"])
        self.assertTrue(stopped["router"] and stopped["sidecar-2"] and stopped["engine-2"])
        self.assertEqual(list(self.fleet.units), [unit])


SLO_RERUN = {"fleet-limits"} | {
    f"{stage}[{host}]" for stage in ("preflight", "router-start") for host in ROUTERS
}


class InputTests(unittest.TestCase):
    def setUp(self):
        self.env = Fleet(Path(tempfile.mkdtemp())).env

    def changed(self, **values: str) -> set[str]:
        before = deploy.inputs(self.env)
        after = deploy.inputs({**self.env, **values})
        return {name for name in before if before[name].digest != after[name].digest}

    def test_each_input_reruns_only_its_own_fact(self):
        self.assertEqual(self.changed(NARWHAL_TTFT_S="5"), {"slo"})
        self.assertEqual(self.changed(NARWHAL_ROUTER_OVERHEAD_S="0.1"), {"slo"})
        self.assertEqual(self.changed(NARWHAL_FIRST_TOKEN_MARGIN_S="1"), {"deadline_margin"})
        self.assertEqual(self.changed(NARWHAL_PROFILE_WARMUP_REQUESTS="4"), {"profiling"})
        self.assertEqual(self.changed(NARWHAL_CALIBRATION_SAMPLES="200"), {"calibration"})
        self.assertEqual(self.changed(NARWHAL_FABRIC_BURST="3"), {"fabric_workload"})
        self.assertEqual(self.changed(NARWHAL_FABRIC_TEST_PORT="5202"), {"fabric_test"})
        self.assertEqual(self.changed(NARWHAL_RDMA_GID_INDEX="3"), {"fabric_test"})
        self.assertEqual(self.changed(NARWHAL_ROUTER_URL="http://10.0.0.1:9000"), {"router"})
        self.assertEqual(self.changed(NARWHAL_LEASE_PATH="/srv/other/lease.json"), {"router"})
        self.assertEqual(self.changed(NARWHAL_GRAFANA_BIND_ADDRESS="127.0.0.1"), {"monitoring"})
        self.assertEqual(self.changed(NARWHAL_FLEET_NAME="fleet-b"), {"monitoring"})
        self.assertEqual(
            self.changed(NARWHAL_ALERTMANAGER_URL="http://10.0.0.9:9093"), {"monitoring"}
        )
        self.assertEqual(self.changed(NARWHAL_LOAD_BALANCER_SSH="deploy@h2"), {"destinations"})
        self.assertEqual(self.changed(NARWHAL_NODE_2_SSH="deploy@h3"), {"destinations"})
        self.assertEqual(self.changed(NARWHAL_MODEL_DIR="/models/other"), {"discovery"})
        self.assertEqual(self.changed(NARWHAL_STAGE_TIMEOUT_SECONDS="60"), set())

    def test_secrets_enter_only_as_digests(self):
        inputs = deploy.inputs({**self.env, "NARWHAL_ENGINE_KEY": "secret-value"})
        self.assertNotIn("secret-value", json.dumps({k: vars(v) for k, v in inputs.items()}))

    def test_every_malformed_input_is_named(self):
        env = {
            **self.env,
            "NARWHAL_ROUTER_URL": "router:8000",
            "NARWHAL_FIRST_TOKEN_MARGIN_S": "0",
            "NARWHAL_CALIBRATION_SAMPLES": "99",
            "NARWHAL_FLEET_NAME": "Fleet A",
            "NARWHAL_ALERTMANAGER_URL": "alertmanager:9093",
        }
        del env["NARWHAL_CALIBRATION_INPUT_TOKENS"]
        with self.assertRaises(deploy.InputError) as caught:
            deploy.inputs(env)
        self.assertEqual(
            set(caught.exception.problems),
            {
                "set NARWHAL_FIRST_TOKEN_MARGIN_S to a positive number",
                "set NARWHAL_CALIBRATION_INPUT_TOKENS to comma-separated positive integers",
                "set NARWHAL_ROUTER_URL to the router's http://HOST:PORT",
                "set NARWHAL_FLEET_NAME to the fleet's name, lowercase letters, digits, '.', '_' "
                "or '-', which labels every series",
                "NARWHAL_ALERTMANAGER_URL 'alertmanager:9093' needs http(s)://HOST:PORT",
                f"set NARWHAL_CALIBRATION_SAMPLES to an integer of at least "
                f"{MIN_SAMPLES_PER_GROUP}",
            },
        )


if __name__ == "__main__":
    unittest.main()
