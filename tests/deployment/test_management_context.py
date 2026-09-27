"""Exercise inherited authority in real children and reject forged or stale grants."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from narwhal.deployment.management_context import (
    CONTEXT_ENV,
    command_context,
    completed_command_exit,
    inherited_context,
)
from narwhal.deployment.management_coordinator import OperationCoordinator
from narwhal.deployment.management_executor import StageContext, worker_identity
from narwhal.deployment.management_plans import PlanStore
from narwhal.deployment.management_records import OperationError
from narwhal.deployment.management_registry import load_registry
from tests.deployment.test_management_plans import FixtureAdapter
from tests.fixtures import ROOT

_CHILD = """
import json, sys
from pathlib import Path
from narwhal.deployment import management_coordinator
from narwhal.deployment.management_context import authorize_command, inherited_context
from narwhal.deployment.management_records import OperationError
from tests.deployment.test_management_plans import FixtureAdapter
management_coordinator.installed_adapters = lambda: {"local-dev-v1": FixtureAdapter()}
try:
    root = Path(sys.argv[1])
    if sys.argv[2] == "cli-conflict":
        from narwhal.dev.cli import main
        def launch(self, target, operation, created):
            return self.run_worker(target, operation["operation_id"])
        management_coordinator.OperationCoordinator._launch = launch
        raise SystemExit(main(["dev", "verify", "--instance", str(root), "--format", "json"]))
    elif sys.argv[2] == "command-error":
        from narwhal.dev import cli
        cli.lifecycle.instance = lambda root: {}
        def broken(root):
            raise OSError("Synthetic command failure")
        cli.lifecycle.verify = broken
        raise SystemExit(cli.main(["dev", "verify", "--instance", str(root), "--format", "json"]))
    elif sys.argv[2] == "preflight":
        print(json.dumps({"authorized": authorize_command("narwhal-check", action="fleet_preflight",
            instance=None, fleet=root / "run-fixture" / "fleet.json")}))
    elif sys.argv[2] == "inspect":
        value = inherited_context(root)
        print(json.dumps({"operation_id": value["owner"]["operation_id"]}))
    else:
        argv = ["dev", "verify", "--instance", str(root)]
        if sys.argv[2] == "forged-args":
            argv += ["--arbitrary", "value"]
        print(json.dumps({"authorized": authorize_command("narwhal", action="dev_verify",
            instance=root, fleet=None, arguments={"argv": argv})}))
except OperationError as error:
    print(json.dumps({"error": error.code}))
    raise SystemExit(2)
"""


class ManagementContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.registry_path = self.root / "registry.json"
        self.document = {
            "schema": "narwhal.management-registry",
            "schema_version": 1,
            "registry_id": str(uuid4()),
            "state_dir": str(self.root / "state"),
            "targets": [
                {
                    "id": "dev",
                    "kind": "dev",
                    "working_directory": str(self.root),
                    "artifact_root": str(self.root / "artifacts"),
                    "fleet_file": None,
                    "instance_dir": str(self.root / "instance"),
                    "adapter": {"id": "local-dev-v1", "settings_path": None},
                    "capabilities": ["inspect", "measure"],
                    "actions": ["dev_verify"],
                }
            ],
        }
        self.save()
        registry = load_registry(self.registry_path)
        self.coordinator = OperationCoordinator(
            registry,
            self.registry_path,
            adapters={"local-dev-v1": FixtureAdapter()},
            launcher=lambda *args: None,
        )
        preparation = self.coordinator.submit_prepare("dev", "dev_verify", {}, str(uuid4()))
        completed = self.coordinator.run_worker("dev", preparation["operation_id"])
        self.assertEqual(completed["state"], "succeeded", completed)
        plan = PlanStore(registry, "dev").read(completed["result"]["data"]["plan_id"])
        submitted = self.coordinator.submit_execute("dev", plan["plan_id"], str(uuid4()))
        record = self.coordinator.store.claim("dev", submitted["operation_id"], worker_identity())
        record["current_stage"] = "verify"
        self.coordinator.store.commit(
            "dev",
            record["operation_id"],
            record,
            expected_revision=record["revision"],
            fence=record["worker"]["fence"],
        )
        self.context = StageContext(
            registry,
            "dev",
            record["operation_id"],
            fence=record["worker"]["fence"],
            stage=plan["payload"]["stages"][0],
        )
        self.command = [
            sys.executable,
            "-m",
            "narwhal.dev.cli",
            "dev",
            "verify",
            "--instance",
            str(self.root / "instance"),
            "--format",
            "json",
        ]

    def save(self):
        self.registry_path.write_text(json.dumps(self.document))
        self.registry_path.chmod(0o600)

    def child(self, credential, mode="authorize", *, env=None, pass_fds=None):
        return subprocess.run(
            [sys.executable, "-c", _CHILD, str(self.root / "instance"), mode],
            cwd=ROOT,
            env={**os.environ, **credential.env, **(env or {})},
            pass_fds=credential.pass_fds if pass_fds is None else pass_fds,
            capture_output=True,
            text=True,
            timeout=5,
        )

    def issue(self, hashes=None):
        return command_context(
            self.context,
            command=self.command,
            input_hashes=hashes or {},
            registry_path=self.registry_path,
        )

    def test_real_child_joins_exact_command_and_cannot_change_arguments(self):
        with self.issue() as credential:
            accepted = self.child(credential)
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            self.assertTrue(json.loads(accepted.stdout)["authorized"])
            changed = self.child(credential, "forged-args")
            self.assertEqual(changed.returncode, 2, changed.stderr)
            self.assertEqual(json.loads(changed.stdout)["error"], "permission_denied")
            descriptor_only = self.child(credential, pass_fds=())
            self.assertEqual(descriptor_only.returncode, 2, descriptor_only.stderr)
        self.assertFalse(list((self.root / "state").glob("command-*.json")))

    def test_authenticated_child_retains_its_text_exit_outside_the_json_result(self):
        with self.issue() as credential:
            child = self.child(credential, "command-error")
        self.assertEqual(child.returncode, 4, child.stderr)
        command = json.loads(child.stdout)
        self.assertEqual(command["status"], "error", command)
        self.assertNotIn("text_exit_code", child.stdout)
        record = self.context.read()
        self.assertEqual(completed_command_exit(self.context.registry, record), 1)
        receipt = self.root / "state" / f"dev-exit-{record['operation_id']}.json"
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
        changed = json.loads(receipt.read_text())
        changed["operation_id"] = str(uuid4())
        receipt.write_text(json.dumps(changed))
        with self.assertRaisesRegex(OperationError, "does not match"):
            completed_command_exit(self.context.registry, record)

    def test_revoked_grant_and_changed_input_fail_inside_child(self):
        source = self.root / "input.json"
        source.write_text("original")
        source.chmod(0o600)
        import hashlib

        with self.issue(
            {str(source): hashlib.sha256(source.read_bytes()).hexdigest()}
        ) as credential:
            source.write_text("replacement")
            changed = self.child(credential, "inspect")
            self.assertEqual(json.loads(changed.stdout)["error"], "stale_plan", changed.stderr)
            self.document["targets"][0]["capabilities"] = ["inspect"]
            self.save()
            revoked = self.child(credential)
            self.assertEqual(
                json.loads(revoked.stdout)["error"], "permission_denied", revoked.stderr
            )

    def test_closed_scope_and_forged_regular_file_cannot_authorize(self):
        with self.issue() as credential:
            descriptor = os.dup(credential.pass_fds[0])
            env = {**credential.env, CONTEXT_ENV: str(descriptor)}
        try:
            with patch.dict(os.environ, env), self.assertRaises(OperationError):
                inherited_context()
        finally:
            os.close(descriptor)
        fake = self.root / "fake.json"
        fake.write_text('{"operation_id":"forged"}')
        fake.chmod(0o600)
        with (
            fake.open("rb") as stream,
            patch.dict(os.environ, {CONTEXT_ENV: str(stream.fileno())}),
            self.assertRaises(OperationError),
        ):
            inherited_context()

    def test_verification_helpers_join_prior_generation_and_legacy_cli_ownership(self):
        instance = self.root / "instance"
        instance.mkdir(mode=0o700)
        run = instance / "run-fixture"
        run.mkdir(mode=0o700)
        for owner in (
            None,
            {"operation_id": str(uuid4()), "stage_id": "up", "launch_token": str(uuid4())},
        ):
            with self.subTest(owner=owner):
                state = {"run": str(run), "management_owner": owner}
                source = instance / "lifecycle.json"
                source.write_text(json.dumps(state))
                source.chmod(0o600)
                if owner is not None:
                    marker = run / "management-owner.json"
                    marker.write_text(json.dumps(owner))
                    marker.chmod(0o600)
                with self.issue() as credential:
                    state["phase"] = "launched"
                    source.write_text(json.dumps(state))
                    checked = self.child(credential, "preflight")
                    self.assertEqual(checked.returncode, 0, checked.stderr + checked.stdout)
                    state["run"] = str(instance / "run-other")
                    source.write_text(json.dumps(state))
                    changed = self.child(credential, "preflight")
                    self.assertEqual(json.loads(changed.stdout)["error"], "permission_denied")

    def test_conflicting_submission_cannot_bypass_current_reservation(self):
        env = {**os.environ, "NARWHAL_MANAGEMENT_REGISTRY": str(self.registry_path)}
        env.pop(CONTEXT_ENV, None)
        child = subprocess.run(
            [sys.executable, "-c", _CHILD, str(self.root / "instance"), "cli-conflict"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(child.returncode, 1, child.stderr)
        result = json.loads(child.stdout)
        self.assertEqual(result["errors"][0]["code"], "resource_busy", result)
        self.assertFalse((self.root / "instance").exists())
