"""Check permission receipts and audit failures at the protected effect boundary."""

import asyncio
import contextlib
import io
import json
import os
import stat
import unittest
from unittest.mock import patch
from uuid import uuid4

from narwhal.deployment.management_access import InspectionAccess
from narwhal.deployment.management_audit import execution_event
from narwhal.deployment.management_coordinator import OperationCoordinator
from narwhal.deployment.management_executor import StageOutcome, _known_helpers_stopped
from narwhal.deployment.management_records import OperationError
from narwhal.deployment.management_registry import ManagementRegistry
from narwhal.dev import cli as dev_cli
from narwhal.diagnostics.management_artifacts import ArtifactStore
from narwhal.mcp.adapters import ToolRegistry
from narwhal.mcp.operations import OperationTools
from tests.deployment import test_management_plans as plan_fixtures


class ExecutionAuditTests(unittest.TestCase):
    setUp = plan_fixtures.PlanTests.setUp
    plan = plan_fixtures.PlanTests.plan

    def path(self):
        return self.registry.state_dir / f"execution-{self.registry.registry_id}.jsonl"

    def rows(self):
        return [json.loads(line) for line in self.path().read_text().splitlines()]

    def save_registry(self):
        path = self.root / "registry.json"
        path.write_text(json.dumps(self.document))
        path.chmod(0o600)
        self.registry = ManagementRegistry.model_validate_json(json.dumps(self.document))
        return path

    def test_accepted_stage_and_terminal_receipts_precede_effects_and_link_evidence(self):
        def launched(target, operation_id):
            row = self.rows()[-1]
            self.assertEqual(
                (row["event"], row["operation_id"]), ("operation_accepted", operation_id)
            )
            self.assertEqual(row["action"], "dev_verify")

        self.coordinator.launcher = launched
        plan = self.plan()
        prepared = [row for row in self.rows() if row["event"] == "operation_finished"][-1]
        self.assertEqual(prepared["plan_id"], plan["plan_id"])
        operation = self.coordinator.submit_execute("dev", plan["plan_id"], str(uuid4()))
        reference = ArtifactStore(str(self.registry.registry_id), self.registry.targets[0]).export(
            b"evidence", kind="fixture"
        )

        def execute(context, stage, plan):
            rows = self.rows()
            self.assertTrue(
                any(
                    row["event"] == "stage_started"
                    and row["stage_id"] == "verify"
                    and row["operation_id"] == operation["operation_id"]
                    for row in rows
                )
            )
            self.assertEqual(rows[-1]["permission"]["decision"], "allow")
            self.assertEqual(
                rows[-1]["permission"]["required_capabilities"], ["inspect", "measure"]
            )
            return StageOutcome(artifacts=[reference])

        with patch.object(self.adapter, "execute_stage", side_effect=execute):
            completed = self.coordinator.run_worker("dev", operation["operation_id"])
        self.assertEqual(completed["state"], "succeeded")
        final = self.rows()[-1]
        self.assertEqual(final["event"], "operation_finished")
        self.assertEqual(final["plan_id"], plan["plan_id"])
        self.assertIn(reference["artifact_id"], final["artifact_ids"])
        self.assertEqual(final["outcome"], "success")
        self.assertEqual(stat.S_IMODE(self.path().stat().st_mode), 0o600)

    def test_capability_denial_is_retained_before_admission(self):
        self.document["targets"][0]["capabilities"] = ["inspect"]
        self.save_registry()
        coordinator = OperationCoordinator(self.registry, adapters={"local-dev-v1": self.adapter})
        with (
            patch.object(coordinator.store, "admit") as admit,
            self.assertRaises(OperationError) as raised,
        ):
            coordinator.submit_prepare("dev", "dev_verify", {}, str(uuid4()))
        self.assertEqual(raised.exception.code, "permission_denied")
        admit.assert_not_called()
        row = self.rows()[-1]
        self.assertEqual(
            row["permission"],
            {
                "decision": "deny",
                "required_capabilities": ["inspect", "measure"],
                "granted_capabilities": ["inspect"],
                "action_allowed": True,
            },
        )
        self.assertEqual(row["error_codes"], ["permission_denied"])

    def test_cli_denial_and_acceptance_use_the_same_coordinator_receipts(self):
        registry_path = self.save_registry()
        with (
            patch.dict(os.environ, {"NARWHAL_MANAGEMENT_REGISTRY": str(registry_path)}),
            patch(
                "narwhal.deployment.management_coordinator.installed_adapters",
                return_value={"local-dev-v1": self.adapter},
            ),
            patch("narwhal.deployment.management_worker.launch_worker"),
            patch(
                "narwhal.deployment.management_cli._wait",
                side_effect=lambda coordinator, target, op: coordinator.run_worker(target, op),
            ),
            patch.object(dev_cli.lifecycle, "verify") as unmanaged,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(
                dev_cli.main(
                    ["--format", "json", "dev", "verify", "--instance", str(self.root / "instance")]
                ),
                0,
            )
            self.document["targets"][0]["capabilities"] = ["inspect"]
            self.save_registry()
            self.assertEqual(
                dev_cli.main(
                    ["--format", "json", "dev", "verify", "--instance", str(self.root / "instance")]
                ),
                2,
            )
        unmanaged.assert_not_called()
        rows = self.rows()
        self.assertTrue(
            any(row["event"] == "operation_accepted" and row["source"] == "cli" for row in rows)
        )
        self.assertTrue(
            any(
                row["event"] == "permission_decision"
                and row["source"] == "cli"
                and row["outcome"] == "denied"
                for row in rows
            )
        )
        self.assertEqual(rows[-1]["error_codes"], ["permission_denied"])

    def test_mcp_inspection_denial_never_invokes_coordinator_and_retains_decision(self):
        self.document["targets"][0]["capabilities"] = ["measure"]
        self.save_registry()
        coordinator = OperationCoordinator(
            self.registry, adapters={"local-dev-v1": self.adapter}, entry_point="mcp"
        )
        tools = OperationTools(self.registry, coordinator)
        with patch.object(coordinator, "submit_prepare") as submit:
            response = asyncio.run(
                ToolRegistry(tools.adapters()).dispatch(
                    "plan_prepare",
                    {
                        "target_id": "dev",
                        "action": "dev_verify",
                        "parameters": {},
                        "request_id": str(uuid4()),
                    },
                )
            )
        self.assertEqual(response["errors"][0]["code"], "permission_denied")
        submit.assert_not_called()
        row = self.rows()[-1]
        self.assertEqual(row["source"], "mcp")
        self.assertEqual(row["permission"]["decision"], "deny")
        self.assertEqual(row["permission"]["required_capabilities"], ["inspect", "measure"])

    def test_mcp_acceptance_retains_request_and_operation_identity(self):
        self.coordinator.entry_point = "mcp"
        tools = OperationTools(self.registry, self.coordinator)
        request_id = str(uuid4())
        response = asyncio.run(
            ToolRegistry(tools.adapters()).dispatch(
                "plan_prepare",
                {
                    "target_id": "dev",
                    "action": "dev_verify",
                    "parameters": {},
                    "request_id": request_id,
                },
            )
        )
        self.assertEqual(response["outcome"], "accepted")
        row = next(row for row in self.rows() if row["event"] == "operation_accepted")
        self.assertEqual(row["source"], "mcp")
        self.assertEqual(row["tool"], "plan_prepare")
        self.assertEqual(row["request_id"], request_id)
        self.assertEqual(row["operation_id"], response["data"]["operation_id"])
        self.assertEqual(self.rows()[-1]["operation_id"], row["operation_id"])

    def test_secrets_unknown_selectors_and_raw_inputs_never_enter_receipts(self):
        secret = "credentialmarker"
        self.document["targets"][0]["credential_env"] = ["FIXTURE_CREDENTIAL"]
        self.save_registry()
        with patch.dict(os.environ, {"FIXTURE_CREDENTIAL": secret}):
            execution_event(
                self.registry,
                "tool_finished",
                source="mcp",
                target_id="private-unknown-target",
                action="dev_verify",
                operation={
                    "current_stage": secret,
                    "arguments": {"prompt": "request-marker"},
                    "path": "/private/host",
                },
                outcome="error",
                codes=[secret],
            )
        raw = self.path().read_text()
        for marker in (secret, "private-unknown-target", "request-marker", "/private/host"):
            self.assertNotIn(marker, raw)
        self.assertIsNone(self.rows()[-1]["target_id"])
        self.assertEqual(self.rows()[-1]["stage_id"], "[REDACTED]")

    def test_unsafe_audit_file_denies_before_admission_or_callback(self):
        self.registry.state_dir.mkdir(mode=0o700)
        outside = self.root / "outside"
        outside.write_text("untouched")
        self.path().symlink_to(outside)
        with (
            patch.object(self.adapter, "prepare") as prepare,
            self.assertRaises(OperationError) as raised,
        ):
            self.coordinator.submit_prepare("dev", "dev_verify", {}, str(uuid4()))
        self.assertEqual(raised.exception.code, "audit_failed")
        prepare.assert_not_called()
        self.assertEqual(outside.read_text(), "untouched")
        self.assertEqual(self.launches, [])

    def test_failed_acceptance_receipt_preserves_queued_identity_and_replay(self):
        request = str(uuid4())
        original = InspectionAccess.write_audit

        def append(access, stream, row):
            if row.get("event") == "operation_accepted":
                raise OSError("private-error-marker")
            return original(access, stream, row)

        with patch.object(InspectionAccess, "write_audit", new=append):
            accepted = self.coordinator.submit_prepare("dev", "dev_verify", {}, request)
        self.assertEqual(accepted["state"], "queued")
        self.assertEqual(self.launches, [])
        repeated = self.coordinator.submit_prepare("dev", "dev_verify", {}, request)
        self.assertEqual(repeated["operation_id"], accepted["operation_id"])
        self.assertEqual(len(self.launches), 1)
        self.assertNotIn("private-error-marker", self.path().read_text())

    def test_stage_audit_failure_prevents_effect_and_terminal_failure_preserves_evidence(self):
        plan = self.plan()
        accepted = self.coordinator.submit_execute("dev", plan["plan_id"], str(uuid4()))
        original = InspectionAccess.write_audit

        def append(access, stream, row):
            if row.get("event") in {"stage_started", "operation_finished"}:
                raise OSError("private-error-marker")
            return original(access, stream, row)

        with (
            patch.object(InspectionAccess, "write_audit", new=append),
            self.assertLogs("narwhal.deployment.management_audit", level="ERROR") as logs,
        ):
            completed = self.coordinator.run_worker("dev", accepted["operation_id"])
        self.assertEqual(completed["state"], "failed")
        self.assertEqual(completed["result"]["errors"][0]["code"], "audit_failed")
        self.assertEqual(self.adapter.executions, 0)
        self.assertTrue(completed["result"]["data"]["summary_artifact_id"])
        self.assertNotIn("private-error-marker", str(logs.output))

    def test_cancellation_receipt_contains_inspect_only_preparation_decision(self):
        operation = self.coordinator.submit_prepare("dev", "dev_verify", {}, str(uuid4()))
        self.coordinator.cancel_operation("dev", operation["operation_id"])
        row = next(row for row in self.rows() if row["event"] == "cancellation_requested")
        self.assertEqual(row["operation_id"], operation["operation_id"])
        self.assertEqual(row["permission"]["required_capabilities"], ["inspect"])
        self.assertEqual(self.rows()[-1]["outcome"], "interrupted")

    def test_terminal_receipt_failure_does_not_erase_committed_success(self):
        plan = self.plan()
        operation = self.coordinator.submit_execute("dev", plan["plan_id"], str(uuid4()))
        original = InspectionAccess.write_audit

        def append(access, stream, row):
            if row.get("event") == "operation_finished":
                raise OSError("private-error-marker")
            return original(access, stream, row)

        with (
            patch.object(InspectionAccess, "write_audit", new=append),
            self.assertLogs("narwhal.deployment.management_audit", level="ERROR"),
        ):
            completed = self.coordinator.run_worker("dev", operation["operation_id"])
        self.assertEqual(completed["state"], "succeeded")
        self.assertEqual(self.adapter.executions, 1)
        self.assertEqual(self.coordinator.store.read("dev", operation["operation_id"]), completed)
        self.assertTrue(completed["result"]["data"]["summary_artifact_id"])

    def test_failed_stage_completion_receipt_stops_the_next_stage(self):
        original_prepare = self.adapter.prepare

        def prepare(*args):
            prepared = original_prepare(*args)
            prepared.stages.append(
                {**prepared.stages[0], "stage_id": "second", "depends_on": ["verify"]}
            )
            return prepared

        with patch.object(self.adapter, "prepare", side_effect=prepare):
            plan = self.plan()
        operation = self.coordinator.submit_execute("dev", plan["plan_id"], str(uuid4()))
        original = InspectionAccess.write_audit

        def append(access, stream, row):
            if row.get("event") == "stage_finished" and row["outcome"] == "success":
                raise OSError("private-error-marker")
            return original(access, stream, row)

        with patch.object(InspectionAccess, "write_audit", new=append):
            completed = self.coordinator.run_worker("dev", operation["operation_id"])
        self.assertEqual(completed["state"], "failed")
        self.assertEqual(completed["result"]["errors"][0]["code"], "audit_failed")
        self.assertEqual(self.adapter.executions, 1)
        self.assertEqual(completed["stages"][1]["state"], "pending")

    def test_remote_helper_requires_adapter_absence_without_local_process_inspection(self):
        receipt = {
            "kind": "measurement_helper",
            "host_id": "remote",
            "effect": "confirmed",
            "identity": {"job_id": str(uuid4()), "supervisor": {"pid": os.getpid()}},
        }
        operation = {"stages": [{"effects": [receipt]}]}
        with patch("narwhal.deployment.management_executor.stages.active") as active:
            self.assertFalse(_known_helpers_stopped(operation))
            receipt["effect"] = "unknown"
            self.assertFalse(_known_helpers_stopped(operation))
            receipt["effect"] = "absent"
            self.assertTrue(_known_helpers_stopped(operation))
        active.assert_not_called()

    def test_local_helper_still_checks_live_identity_after_an_absence_receipt(self):
        identity = {"pid": os.getpid()}
        operation = {
            "stages": [
                {
                    "effects": [
                        {
                            "kind": "measurement_helper",
                            "host_id": "management",
                            "effect": "absent",
                            "identity": identity,
                        }
                    ]
                }
            ]
        }
        with patch(
            "narwhal.deployment.management_executor.stages.active", return_value=True
        ) as active:
            self.assertFalse(_known_helpers_stopped(operation))
        active.assert_called_once_with(identity)
