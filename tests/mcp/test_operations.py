"""Exercise durable operation views through the production tool dispatch layer."""

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from narwhal.deployment.management_coordinator import OperationCoordinator, View
from narwhal.deployment.management_records import OperationError
from narwhal.deployment.management_registry import ManagementRegistry
from narwhal.deployment.management_store import OperationStore
from narwhal.mcp.adapters import ToolRegistry
from narwhal.mcp.inspection import inspection_adapters
from narwhal.mcp.operations import OperationTools


class OperationToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.document = {
            "schema": "narwhal.management-registry",
            "schema_version": 1,
            "registry_id": str(uuid4()),
            "state_dir": str(self.root / "state"),
            "targets": [
                {
                    "id": "local",
                    "kind": "dev",
                    "working_directory": str(self.root),
                    "artifact_root": str(self.root / "artifacts"),
                    "fleet_file": None,
                    "instance_dir": str(self.root / "instance"),
                    "adapter": {"id": "local-dev-v1", "settings_path": None},
                    "capabilities": ["inspect", "measure", "mutate"],
                    "actions": ["dev_verify"],
                }
            ],
        }
        self.configure()

    def configure(self):
        self.registry = ManagementRegistry.model_validate_json(json.dumps(self.document))
        self.coordinator = OperationCoordinator(self.registry, adapters={})
        self.store = OperationStore(self.registry)
        self.tools = OperationTools(self.registry, self.coordinator)
        self.dispatcher = ToolRegistry(
            (*inspection_adapters(self.registry), *self.tools.adapters())
        )

    def admit(self):
        record, created = self.store.admit(
            target_id="local",
            request_id=str(uuid4()),
            tool="plan_prepare",
            action="dev_verify",
            parameters={},
        )
        self.assertTrue(created)
        return record["operation_id"]

    async def call(self, name, **arguments):
        return await self.dispatcher.dispatch(name, {"target_id": "local", **arguments})

    def receipts(self):
        path = self.root / "state" / f"inspection-{self.registry.registry_id}.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()]

    async def test_installed_discovery_contains_only_store_tools_and_inspection(self):
        names = {adapter.name for adapter in self.dispatcher.adapters()}
        self.assertEqual(
            names,
            {
                "target_list",
                "config_inspect",
                "config_validate",
                "fleet_status",
                "dev_status",
                "diagnostics_collect",
                "artifact_read",
                "operation_list",
                "operation_inspect",
                "operation_cancel",
                "plan_inspect",
            },
        )
        response = await self.call("operation_list")
        self.assertEqual(response["outcome"], "success", response)
        self.assertEqual(response["data"], {"operations": [], "next_cursor": None})
        for adapter in self.tools.adapters():
            self.assertEqual(adapter.read_only, adapter.name != "operation_cancel")

    async def test_list_snapshot_inspection_and_cancellation_survive_frontend_restart(self):
        first = self.admit()
        second = self.admit()
        page = await self.call("operation_list", limit=1)
        self.assertEqual(page["outcome"], "success", page)
        self.assertEqual(page["data"]["operations"][0]["operation_id"], second)
        cursor = page["data"]["next_cursor"]
        self.assertIsNotNone(cursor)
        self.admit()
        self.configure()
        next_page = await self.call("operation_list", limit=1, cursor=cursor)
        self.assertEqual(next_page["data"]["operations"][0]["operation_id"], first)
        self.assertIsNone(next_page["data"]["next_cursor"])
        mismatched = await self.call("operation_list", limit=2, cursor=cursor)
        self.assertEqual(mismatched["errors"][0]["code"], "invalid_cursor", mismatched)

        inspected = await self.call("operation_inspect", operation_id=first)
        self.assertEqual(inspected["outcome"], "success", inspected)
        self.assertEqual(inspected["data"]["operation"]["state"], "queued")
        self.assertIsNone(inspected["data"]["operation"]["result_status"])
        snapshot_id = inspected["data"]["record_artifact_id"]
        cancelled = await self.call("operation_cancel", operation_id=first)
        self.assertEqual(cancelled["outcome"], "success", cancelled)
        self.assertEqual(cancelled["data"]["operation"]["state"], "cancelled")
        self.assertEqual(cancelled["data"]["operation"]["result_status"], "interrupted")
        self.assertIsNotNone(self.store.read("local", first)["cancellation"]["requested_at"])

        frozen = await self.call("artifact_read", artifact_id=snapshot_id)
        self.assertEqual(frozen["outcome"], "success", frozen)
        old = json.loads(frozen["data"]["text"])
        self.assertIsNone(old["cancellation"]["requested_at"])
        again = await self.call("operation_cancel", operation_id=first)
        self.assertEqual(
            again["data"]["operation"]["revision"], cancelled["data"]["operation"]["revision"]
        )

    async def test_invalid_arguments_and_revoked_inspection_do_not_reach_coordinator(self):
        with patch.object(self.coordinator, "inspect_operation") as inspect:
            for arguments in (
                {"operation_id": "bad"},
                {"operation_id": str(uuid4()), "command": "ignored"},
            ):
                rejected = await self.call("operation_inspect", **arguments)
                self.assertEqual(rejected["errors"][0]["code"], "invalid_arguments", rejected)
            inspect.assert_not_called()
        self.document["targets"][0]["capabilities"] = []
        self.configure()
        with patch.object(self.coordinator, "list_operations") as listing:
            rejected = await self.call("operation_list")
            self.assertEqual(rejected["errors"][0]["code"], "permission_denied", rejected)
            listing.assert_not_called()

    async def test_missing_records_and_core_failures_preserve_stable_outcomes(self):
        for name, identity in (
            ("operation_inspect", "operation_id"),
            ("operation_cancel", "operation_id"),
            ("plan_inspect", "plan_id"),
        ):
            with self.subTest(name=name):
                missing = await self.call(name, **{identity: str(uuid4())})
                self.assertEqual(missing["outcome"], "invalid_input", missing)
        for code, outcome in (
            ("recovery_required", "failed_gate"),
            ("stale_plan", "failed_gate"),
            ("plan_evidence_changed", "failed_gate"),
            ("operation_record_removed", "error"),
        ):
            with (
                self.subTest(code=code),
                patch.object(
                    self.coordinator,
                    "inspect_operation",
                    side_effect=OperationError(code, "Safe failure"),
                ),
            ):
                failed = await self.call("operation_inspect", operation_id=str(uuid4()))
                self.assertEqual(failed["outcome"], outcome, failed)
                self.assertEqual(failed["errors"][0]["code"], code, failed)

    async def test_unexpected_core_errors_retain_safe_completion_receipts(self):
        secret = "https://private-host.invalid/token?password=secret-value"
        for exception, code in (
            (OSError(secret), "source_unavailable"),
            (RuntimeError(secret), "adapter_failed"),
        ):
            with (
                self.subTest(code=code),
                patch.object(self.coordinator, "list_operations", side_effect=exception),
            ):
                failed = await self.call("operation_list")
            self.assertEqual(failed["outcome"], "error", failed)
            self.assertEqual(failed["errors"][0]["code"], code, failed)
            self.assertNotIn(secret, json.dumps(failed))
            receipts = self.receipts()[-2:]
            self.assertEqual([row["outcome"] for row in receipts], ["started", "error"])
            self.assertEqual(receipts[-1]["error_codes"], [code])
            self.assertNotIn(secret, json.dumps(receipts))

    async def test_unknown_target_is_absent_from_audit_receipts(self):
        alias = "unregistered-private-customer"
        failed = await self.call("operation_list", target_id=alias)
        self.assertEqual(failed["errors"][0]["code"], "target_not_found", failed)
        self.assertIsNone(failed["target_id"])
        self.assertEqual([row["target_id"] for row in self.receipts()], [None, None])
        self.assertNotIn(alias, json.dumps(self.receipts()))

    async def test_cancelled_exchange_records_interruption_without_stopping_core_work(self):
        started, release = threading.Event(), threading.Event()

        def listing(*args):
            started.set()
            release.wait(5)
            return View({"operations": [], "next_cursor": None}, [])

        with patch.object(self.coordinator, "list_operations", side_effect=listing):
            task = asyncio.create_task(self.call("operation_list"))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                receipts = self.receipts()
                self.assertEqual([row["outcome"] for row in receipts], ["started", "interrupted"])
                self.assertEqual(receipts[-1]["error_codes"], ["stage_cancelled"])
            finally:
                release.set()

    async def test_unavailable_start_audit_prevents_dispatch(self):
        with (
            patch.object(self.tools.access, "audit", side_effect=OSError("private-path")),
            patch.object(self.coordinator, "list_operations") as listing,
        ):
            failed = await self.call("operation_list")
        listing.assert_not_called()
        self.assertEqual(failed["outcome"], "invalid_input", failed)
        self.assertEqual(failed["errors"][0]["code"], "permission_denied", failed)
        self.assertNotIn("private-path", json.dumps(failed))

    async def test_completion_audit_failure_preserves_accepted_operation_identity(self):
        self.coordinator.adapters = {"fixture": object()}
        self.dispatcher = ToolRegistry(self.tools.adapters())
        operation_id = str(uuid4())
        with (
            patch.object(self.tools.access, "audit", side_effect=[None, OSError("private-path")]),
            patch.object(
                self.coordinator, "submit_prepare", return_value={"operation_id": operation_id}
            ),
        ):
            failed = await self.call(
                "plan_prepare", action="dev_verify", parameters={}, request_id=str(uuid4())
            )
        self.assertEqual(failed["outcome"], "error", failed)
        self.assertEqual(failed["data"], {"operation_id": operation_id})
        self.assertEqual(failed["errors"][0]["code"], "audit_failed", failed)
        self.assertNotIn("private-path", json.dumps(failed))

    async def test_submission_catalogue_requires_an_explicit_adapter(self):
        self.coordinator.adapters = {"fixture": object()}
        self.dispatcher = ToolRegistry(self.tools.adapters())
        prepare = next(
            adapter for adapter in self.tools.adapters() if adapter.name == "plan_prepare"
        )
        self.assertEqual(prepare.input_model.model_fields["timeout_s"].default, 5)
        operation_id = str(uuid4())
        request_id = str(uuid4())
        with patch.object(
            self.coordinator, "submit_prepare", return_value={"operation_id": operation_id}
        ) as submit:
            accepted = await self.call(
                "plan_prepare", action="dev_verify", parameters={}, request_id=request_id
            )
            self.assertEqual(accepted["outcome"], "accepted", accepted)
            self.assertEqual(accepted["data"], {"operation_id": operation_id})
            submit.assert_called_once_with("local", "dev_verify", {}, request_id)

    async def test_large_plan_view_exports_the_complete_result_within_response_bounds(self):
        data = {"plan": {"retained": "x" * 300_000}, "locally_stale": False}
        with patch.object(self.coordinator, "inspect_plan", return_value=View(data, [])):
            response = await self.call("plan_inspect", plan_id=str(uuid4()))
        self.assertEqual(response["outcome"], "degraded", response)
        self.assertEqual(response["errors"][0]["code"], "result_too_large", response)
        self.assertLessEqual(len(json.dumps(response).encode()), 262_144)
        retained, offset = "", 0
        while offset is not None:
            page = await self.call(
                "artifact_read", artifact_id=response["data"]["result_artifact_id"], offset=offset
            )
            self.assertEqual(page["outcome"], "success", page)
            retained += page["data"]["text"]
            offset = page["data"]["next_offset"]
        self.assertEqual(json.loads(retained)["data"], data)
