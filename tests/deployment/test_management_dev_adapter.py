"""Exercise dev adapter receipts with local CPU processes and fixed CLI outcomes."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from narwhal import command_results, contracts
from narwhal.deployment import native_engine, stages
from narwhal.deployment.management_access import InspectionAccess
from narwhal.deployment.management_records import OperationError
from narwhal.deployment.management_registry import ManagementRegistry
from narwhal.dev import lifecycle
from narwhal.dev.management_adapter import LocalDevAdapter, _services


class DevAdapterTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.instance = self.root / "instance"
        self.artifacts = self.root / "artifacts"
        self.registry = ManagementRegistry.model_validate_json(
            json.dumps(
                {
                    "schema": "narwhal.management-registry",
                    "schema_version": 1,
                    "registry_id": str(uuid4()),
                    "state_dir": str(self.root / "state"),
                    "targets": [
                        {
                            "id": "dev",
                            "kind": "dev",
                            "working_directory": str(self.root),
                            "artifact_root": str(self.artifacts),
                            "instance_dir": str(self.instance),
                            "fleet_file": None,
                            "adapter": {"id": "local-dev-v1", "settings_path": None},
                            "capabilities": ["inspect", "measure", "mutate"],
                            "actions": ["dev_init", "dev_up", "dev_verify", "dev_down"],
                        }
                    ],
                }
            )
        )
        self.owner = {
            "operation_id": str(uuid4()),
            "stage_id": "dev-up",
            "launch_token": str(uuid4()),
        }
        access = InspectionAccess(self.registry)
        self.effects = []
        self.context = SimpleNamespace(
            target=self.registry.targets[0],
            registry=self.registry,
            target_id="dev",
            operation_id=self.owner["operation_id"],
            stage_id=self.owner["stage_id"],
            access=access,
            redactor=access.redactor(self.registry.targets[0]),
            output_dir=self.artifacts / ("operation-" + self.owner["operation_id"]),
            assert_current=lambda: None,
        )
        self.context.record_intent = self.record
        self.context.record_effect = self.record
        self.context.read = lambda: {
            "stages": [{"stage_id": "dev-up", "state": "running", "effects": self.effects}]
        }
        self.children = []
        self.addCleanup(self.stop)
        self.adapter = LocalDevAdapter()

    def stop(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=3)

    def record(self, receipt):
        self.effects[:] = [
            item for item in self.effects if item["resource_id"] != receipt["resource_id"]
        ] + [receipt]

    def initialize(self):
        self.instance.mkdir(mode=0o700, exist_ok=True)
        for name, document in {
            "instance.json": {"engine_count": 2},
            "template.json": {},
            "fleet.json": {},
            "engine-launch.json": {},
        }.items():
            lifecycle.write(self.instance / name, document)

    def services(self):
        self.initialize()
        run = self.instance / "run-123456789abc"
        run.mkdir(mode=0o700)
        state = {
            "schema_version": 1,
            "phase": "launched",
            "run": str(run),
            "processes": [],
            "management_owner": self.owner,
        }
        lifecycle.write(run / "management-owner.json", self.owner)
        for name in ("engine-1", "engine-2", "sidecar-1", "sidecar-2", "router"):
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
            )
            self.children.append(child)
            identity = native_engine.process_identity(child.pid)
            if name.startswith("engine-"):
                (run / name).mkdir(mode=0o700)
                native_engine.write_process_identity(run / name, identity)
                lifecycle.write(run / name / "management-owner.json", self.owner)
            else:
                state["processes"].append(
                    {"name": name, "identity": identity, "management_owner": self.owner}
                )
        lifecycle.write(self.instance / "lifecycle.json", state)
        return run

    def result(self, action, status="success", *, data=None):
        document = contracts.versioned(
            contracts.COMMAND_RESULT,
            {
                "command": "narwhal",
                "operation": "dev " + action,
                "status": status,
                "exit_code": command_results.EXIT_CODES[status],
                "data": data or {},
                "errors": []
                if status == "success"
                else [
                    {
                        "code": "fixture_failure",
                        "message": "CPU fixture outcome",
                        "command": "narwhal",
                    }
                ],
                "artifacts": [
                    {
                        "kind": "instance",
                        "path": str(self.instance / "instance.json"),
                        "state": "existing",
                    }
                ],
            },
        )
        return subprocess.CompletedProcess([], document["exit_code"], json.dumps(document), "")

    def execute(self, action, runner):
        self.context.run_command = runner
        config = {
            "arguments": ["dev", action, "--instance", str(self.instance)],
            "input_hashes": {},
            "template": None,
            "fleet_document": {},
        }
        module = ModuleType("narwhal.dev.management_prepare")
        module.execution_config = lambda *args: config

        @contextlib.contextmanager
        def credential(*args, **kwargs):
            yield SimpleNamespace(owner=self.owner, env={}, pass_fds=())

        with (
            patch.dict(sys.modules, {module.__name__: module}),
            patch("narwhal.dev.management_adapter.command_context", credential),
        ):
            return self.adapter.execute_stage(
                self.context,
                {"operation": "dev." + action},
                {"payload": {"action": "dev_" + action}},
            )

    def test_init_uses_fixed_installed_entrypoint_and_exports_command_result(self):
        commands = []

        def run(command, **kwargs):
            commands.append((command, kwargs))
            self.initialize()
            return self.result("init", data={"status": "initialized"})

        outcome = self.execute("init", run)
        self.assertEqual(commands[0][0][:3], [sys.executable, "-m", "narwhal.dev.cli"])
        self.assertEqual(commands[0][0][-2:], ["--format", "json"])
        self.assertIsNone(commands[0][1]["retain_on_success"])
        self.assertEqual(outcome.status, "success")
        self.assertEqual(outcome.command_result["data"]["status"], "initialized")
        self.assertTrue(any(row["kind"] == "command_result" for row in outcome.artifacts))
        self.assertEqual(self.effects[0]["effect"], "confirmed")

    def test_cli_outcomes_keep_status_exit_error_and_original_artifact(self):
        self.initialize()
        for status in ("failed_gate", "degraded", "invalid_input", "interrupted"):
            with self.subTest(status=status):
                outcome = self.execute(
                    "verify", lambda *args, status=status, **kwargs: self.result("verify", status)
                )
                self.assertEqual(outcome.status, status)
                self.assertEqual(
                    outcome.command_result["exit_code"], command_results.EXIT_CODES[status]
                )
                self.assertEqual(outcome.errors[0]["code"], "fixture_failure")
                self.assertEqual(
                    outcome.command_result["artifacts"][0]["path"],
                    str(self.instance / "instance.json"),
                )

    def test_up_transfers_only_matching_durable_service_receipts(self):
        self.services()
        transferred = []

        def run(command, **kwargs):
            transferred.extend(kwargs["retain_on_success"]({"returncode": 0}))
            for receipt in transferred:
                self.record(receipt)
            return self.result("up", data={"status": "launched"})

        outcome = self.execute("up", run)
        self.assertEqual(outcome.status, "success")
        self.assertEqual(len(transferred), 5)
        self.assertEqual({row["kind"] for row in transferred}, {"engine", "attestation", "router"})
        for receipt in transferred:
            self.assertEqual(receipt["owner"], self.owner)
            self.assertEqual(receipt["effect"], "confirmed")
            self.assertTrue(stages.active(receipt["identity"]))

    def test_up_refuses_service_receipt_from_another_launch(self):
        run = self.services()
        lifecycle.write(
            run / "engine-1" / "management-owner.json", {**self.owner, "launch_token": str(uuid4())}
        )
        with self.assertRaises(OperationError) as error:
            _services(self.context, self.owner, starting=True, require_live=True)
        self.assertEqual(error.exception.code, "recovery_required")

    def test_reconciliation_reads_live_and_stopped_services_without_signalling(self):
        self.services()
        effects = _services(self.context, self.owner, starting=True, require_live=True)
        self.children[0].terminate()
        self.children[0].wait(timeout=3)
        operation = {
            "tool": "plan_execute",
            "stages": [{"state": "running", "effects": effects}],
        }
        with (
            patch.object(stages, "_signal", side_effect=AssertionError("read only")),
            patch.object(native_engine, "_terminate", side_effect=AssertionError("read only")),
        ):
            outcome = self.adapter.reconcile(self.context, operation)
        self.assertEqual(outcome.effects[0]["effect"], "absent")
        self.assertTrue(all(row["effect"] == "confirmed" for row in outcome.effects[1:]))
        self.assertTrue(all(child.poll() is None for child in self.children[1:]))

    def test_symlink_generation_is_rejected_before_process_inspection(self):
        run = self.services()
        moved = self.root / "moved"
        run.rename(moved)
        run.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(ValueError):
            _services(self.context, self.owner, starting=True, require_live=True)

    def test_managed_down_rejects_outside_generation_before_cleanup(self):
        self.initialize()
        outside = self.root / "run-123456789abc"
        outside.mkdir(mode=0o700)
        lifecycle.write(
            self.instance / "lifecycle.json",
            {
                "schema_version": 1,
                "phase": "launched",
                "run": str(outside),
                "processes": [],
            },
        )
        with (
            patch.object(lifecycle, "instance", return_value={}),
            patch(
                "narwhal.deployment.management_context.inherited_context",
                return_value={"owner": self.owner},
            ),
            patch.object(lifecycle, "_stop") as stop,
            self.assertRaisesRegex(ValueError, "within its instance"),
        ):
            lifecycle.down(self.instance)
        stop.assert_not_called()

    def test_nested_helpers_receive_the_authenticated_descriptor(self):
        self.initialize()
        with (
            patch("narwhal.deployment.management_context.inherited_fds", return_value=(123,)),
            patch.object(stages, "run", return_value=subprocess.CompletedProcess([], 0)) as execute,
        ):
            lifecycle._run(self.instance, "narwhal.profiling.probe", [], "profile")
        self.assertEqual(execute.call_args.kwargs["pass_fds"], (123,))

    def test_native_ownership_commit_flushes_file_and_directory(self):
        folder = self.root / "native"
        folder.mkdir()
        with patch("narwhal.deployment.native_engine.os.fsync", wraps=os.fsync) as fsync:
            native_engine.write_process_identity(
                folder, {"pid": 123, "boot_id": str(uuid4()), "start_ticks": 4}
            )
        self.assertEqual(fsync.call_count, 2)
        self.assertEqual((folder / "native-process.json").stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
