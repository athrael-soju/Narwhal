"""Keep registry-bound CLI calls outside lifecycle and measurement callbacks."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from narwhal.deployment import launch_engine
from narwhal.dev import cli as dev_cli
from narwhal.diagnostics import check
from narwhal.profiling import probe
from tests.fixtures import ROOT


class ManagedCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.instance = self.root / "instance"
        self.fleet = self.root / "fleet.json"
        self.fleet.write_bytes((ROOT / "config/fleet.example.json").read_bytes())
        self.fleet.chmod(0o600)
        self.registry = self.root / "registry.json"
        self.document = {
            "schema": "narwhal.management-registry",
            "schema_version": 1,
            "registry_id": "10000000-0000-4000-8000-000000000001",
            "state_dir": str(self.root / "state"),
            "targets": [
                {
                    "id": "dev",
                    "kind": "dev",
                    "working_directory": str(self.root),
                    "artifact_root": str(self.root / "dev-artifacts"),
                    "fleet_file": None,
                    "instance_dir": str(self.instance),
                    "adapter": {"id": "local-dev-v1", "settings_path": None},
                    "capabilities": ["inspect", "measure", "mutate"],
                    "actions": ["dev_init", "dev_up", "dev_verify", "dev_down"],
                },
                {
                    "id": "fleet",
                    "kind": "fleet",
                    "working_directory": str(self.root),
                    "artifact_root": str(self.root / "fleet-artifacts"),
                    "fleet_file": str(self.fleet),
                    "instance_dir": None,
                    "adapter": {"id": "ssh-v1", "settings_path": str(self.root / "site.json")},
                    "capabilities": ["inspect", "measure", "mutate"],
                    "actions": ["fleet_profile", "fleet_preflight"],
                },
            ],
        }
        self.save()
        self.environment = patch.dict(
            os.environ, {"NARWHAL_MANAGEMENT_REGISTRY": str(self.registry)}
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def save(self):
        self.registry.write_text(json.dumps(self.document))
        self.registry.chmod(0o600)

    def invoke(self, function, arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = function([*arguments, "--format", "json"])
        document = json.loads(stdout.getvalue())
        self.assertEqual(document["exit_code"], code)
        return document

    def test_dev_actions_check_binding_before_instance_reads_or_callbacks(self):
        with (
            patch.object(dev_cli.lifecycle, "instance") as instance,
            patch.object(dev_cli.template, "materialize") as materialize,
            patch.object(dev_cli.lifecycle, "up") as up,
            patch.object(dev_cli.lifecycle, "verify") as verify,
            patch.object(dev_cli.lifecycle, "down") as down,
            patch.dict(os.environ, {"NARWHAL_OPERATION_ID": "claimed-parent"}),
        ):
            for action in ("init", "up", "verify", "down"):
                with self.subTest(action=action):
                    result = self.invoke(
                        dev_cli.main, ["dev", action, "--instance", str(self.instance)]
                    )
                    self.assertEqual(result["status"], "failed_gate", result)
                    self.assertEqual(result["errors"][0]["code"], "adapter_unavailable", result)
                    self.assertEqual(result["artifacts"], [])
            for callback in (instance, materialize, up, verify, down):
                callback.assert_not_called()
        self.assertFalse(self.instance.exists())

    def test_dev_permissions_and_unique_target_mapping_precede_adapter_lookup(self):
        cases = ("missing", "ambiguous", "no_inspect", "no_measure", "no_action", "empty_registry")
        for case in cases:
            with self.subTest(case=case), patch.dict(os.environ, {}, clear=False):
                document = json.loads(json.dumps(self.document))
                target = document["targets"][0]
                expected = "permission_denied"
                if case == "missing":
                    target["instance_dir"] = str(self.root / "different")
                    expected = "target_not_found"
                elif case == "ambiguous":
                    document["targets"].append({**target, "id": "alias"})
                    expected = "invalid_input"
                elif case == "no_action":
                    target["actions"] = []
                elif case.startswith("no_"):
                    target["capabilities"].remove(case.removeprefix("no_"))
                else:
                    os.environ["NARWHAL_MANAGEMENT_REGISTRY"] = ""
                    expected = "invalid_input"
                self.registry.write_text(json.dumps(document))
                result = self.invoke(
                    dev_cli.main, ["dev", "verify", "--instance", str(self.instance)]
                )
                self.assertEqual(result["status"], "invalid_input", result)
                self.assertEqual(result["errors"][0]["code"], expected, result)

    def test_profile_modes_and_active_preflight_never_load_or_measure_a_fleet(self):
        cases = (
            (probe, []),
            (probe, ["--refit-samples", "samples.json", "--out", "refit.json"]),
            (probe, ["--merge", "first.json", "--merge", "second.json", "--out", "merged.json"]),
            (check, []),
            (check, ["--no-kv"]),
        )
        for module, arguments in cases:
            with (
                self.subTest(command=module.__name__, arguments=arguments),
                patch.object(module.FleetConfig, "load") as load,
                patch.object(module, "run", new_callable=AsyncMock) as run,
            ):
                result = self.invoke(module.main, ["--fleet", str(self.fleet), *arguments])
                self.assertEqual(result["errors"][0]["code"], "adapter_unavailable", result)
                self.assertEqual(result["status"], "failed_gate", result)
                load.assert_not_called()
                run.assert_not_called()

    def test_engine_actions_and_internal_probes_cannot_bypass_the_binding(self):
        for arguments in (
            ["prepare", "--out", str(self.root / "run")],
            ["check", "--run", str(self.root / "run")],
            ["start", "--run", str(self.root / "run")],
            ["stop-native", "--run", str(self.root / "run")],
        ):
            with (
                self.subTest(arguments=arguments),
                patch.object(launch_engine, "prepare") as prepare,
                patch.object(launch_engine, "load") as load,
            ):
                result = self.invoke(launch_engine.main, arguments)
                self.assertEqual(result["errors"][0]["code"], "adapter_unavailable", result)
                prepare.assert_not_called()
                load.assert_not_called()
        with (
            patch.object(launch_engine, "runtime_cache_probe") as callback,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(launch_engine.main(["_cache-probe", "--plan", "unused.json"]), 1)
            callback.assert_not_called()

    def test_standalone_launcher_rejects_bound_execution_without_package_imports(self):
        launcher = self.root / "launch_engine.py"
        launcher.write_bytes((ROOT / "src/narwhal/deployment/launch_engine.py").read_bytes())
        result = subprocess.run(
            [sys.executable, "-I", "-S", str(launcher), "prepare", "--out", str(self.root / "run")],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("adapter_unavailable", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse((self.root / "run").exists())

    def test_read_only_commands_and_help_ignore_execution_binding(self):
        with patch.dict(os.environ, {"NARWHAL_MANAGEMENT_REGISTRY": "missing-registry.json"}):
            result = self.invoke(dev_cli.main, ["config", "validate", "--fleet", str(self.fleet)])
            self.assertEqual(result["status"], "success", result)
            with (
                patch.object(dev_cli.lifecycle, "instance"),
                patch.object(
                    dev_cli.lifecycle, "status", return_value={"status": "launched"}
                ) as status,
            ):
                result = self.invoke(
                    dev_cli.main, ["dev", "status", "--instance", str(self.instance)]
                )
                self.assertEqual(result["status"], "success", result)
                status.assert_called_once()
            with (
                patch.object(
                    check.FleetConfig,
                    "load",
                    return_value=SimpleNamespace(engine_api_key_env="TEST_KEY"),
                ),
                patch.object(
                    check, "verify_directed_kv_evidence", new_callable=AsyncMock, return_value=[]
                ) as verify,
            ):
                result = self.invoke(
                    check.main, ["--fleet", str(self.fleet), "--verify-evidence", "evidence.json"]
                )
                self.assertEqual(result["status"], "success", result)
                verify.assert_awaited_once()
            for option in ("--print-example-config", "--print-contract-versions"):
                self.assertEqual(self.invoke(check.main, [option])["status"], "success")
            for function in (dev_cli.main, probe.main, check.main, launch_engine.main):
                for option in ("--help", "--version"):
                    with (
                        self.subTest(function=function, option=option),
                        contextlib.redirect_stdout(io.StringIO()),
                        self.assertRaises(SystemExit) as stopped,
                    ):
                        function([option])
                    self.assertEqual(stopped.exception.code, 0)

    def test_unbound_dev_action_retains_existing_dispatch(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(dev_cli.lifecycle, "instance"),
            patch.object(dev_cli.lifecycle, "up", return_value={"status": "launched"}) as up,
        ):
            result = self.invoke(dev_cli.main, ["dev", "up", "--instance", str(self.instance)])
            self.assertEqual(result["status"], "success", result)
            up.assert_called_once_with(self.instance)
