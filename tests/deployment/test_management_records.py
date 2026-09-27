"""Validate operation contract invariants without coercing persisted evidence."""

import copy
import unittest
from uuid import uuid4

from narwhal.contracts import COMMAND_RESULT, ContractVersionError, versioned
from narwhal.deployment import management_records as records
from tests.deployment.test_management_store import execution_plan, identity, terminal


class ManagementRecordTests(unittest.TestCase):
    def operation(self):
        return records.new_operation(
            target_id="local-dev",
            request_id=str(uuid4()),
            tool="plan_execute",
            action="dev_up",
            plan=execution_plan(),
        )

    def test_canonical_encoding_rejects_floats_nonfinite_values_and_nonjson(self):
        self.assertEqual(
            records.canonical({"z": [True, None], "a": "é"}), '{"a":"é","z":[true,null]}'.encode()
        )
        for value in (1.0, float("nan"), {"nested": [float("inf")]}, (1, 2), {1: "value"}):
            with self.subTest(value=value), self.assertRaises(records.OperationError):
                records.canonical(value)

    def test_required_fields_versions_and_compatible_fields(self):
        original = self.operation()
        for name in original:
            value = copy.deepcopy(original)
            del value[name]
            with self.subTest(name=name), self.assertRaises(records.OperationError):
                records.validate_record(value)
        for version in (True, 2):
            with self.subTest(version=version), self.assertRaises(ContractVersionError):
                records.validate_record({**original, "schema_version": version})
        original["future_optional"] = {"elapsed": 1.25}
        records.validate_record(original)
        self.assertIn(b'"elapsed":1.25', records.encode_record(original))

    def test_terminal_requires_evidence_and_known_effects(self):
        operation = self.operation()
        ended = terminal(operation)
        records.validate_record(ended)
        for mutation in ({"result": None}, {"finished_at": None}, {"current_stage": "launch"}):
            with self.subTest(mutation=mutation), self.assertRaises(records.OperationError):
                records.validate_record({**ended, **mutation})
        effect = {
            "resource_id": "engine:0",
            "kind": "engine",
            "host_id": "local",
            "owner": {
                "operation_id": operation["operation_id"],
                "stage_id": "launch",
                "launch_token": None,
            },
            "identity": None,
            "effect": "unknown",
            "observed_at": records.utc_now(),
        }
        ended["stages"][0]["effects"] = [effect]
        with self.assertRaises(records.OperationError):
            records.validate_record(ended)

    def test_command_evidence_allows_finite_numbers_preserves_unknown_codes(self):
        operation = self.operation()
        operation["stages"][0]["command_result"] = versioned(
            COMMAND_RESULT,
            {
                "status": "error",
                "exit_code": 4,
                "data": {"seconds": 0.25},
                "errors": [{"code": "future_code", "message": "retained"}],
                "artifacts": [],
            },
        )
        records.validate_record(operation)
        operation["stages"][0]["command_result"]["data"]["seconds"] = float("nan")
        with self.assertRaises(records.OperationError):
            records.validate_record(operation)

    def test_summary_bounds_errors_and_excludes_private_payloads(self):
        ended = terminal(self.operation())
        ended["result"]["errors"] = [
            {"code": f"code-{number}", "message": "private detail"} for number in range(20)
        ]
        ended["private"] = "private detail"
        summary = records.summary(ended)
        self.assertEqual(len(summary["error_codes"]), 10)
        self.assertEqual(summary["error_count"], 20)
        self.assertNotIn("private detail", str(summary))
        self.assertEqual(len(summary), 16)

    def test_running_requires_worker_budget_and_terminal_success_requires_all_stages(self):
        operation = self.operation()
        operation["state"] = "running"
        with self.assertRaises(records.OperationError):
            records.validate_record(operation)
        operation.update(
            worker={**identity(), "fence": 1},
            started_at=records.utc_now(),
            deadline_at=records.utc_now(),
        )
        records.validate_record(operation)
        ended = terminal(operation, "succeeded")
        ended["stages"][0]["state"] = "pending"
        with self.assertRaises(records.OperationError):
            records.validate_record(ended)
