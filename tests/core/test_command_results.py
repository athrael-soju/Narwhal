"""Exercise installed CLI semantics with local files and substituted engine responses."""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch

from narwhal import command_results as results
from narwhal.config import FleetConfig
from narwhal.contracts import COMMAND_RESULT, ContractVersionError, manifest, validate_document
from narwhal.deployment import launch_engine
from narwhal.dev import cli as dev
from narwhal.diagnostics import check
from narwhal.profiling import probe
from tests.deployment.fixtures import launcher_inputs
from tests.fixtures import ROOT


def call(command, arguments):
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = command([*arguments, "--format", "json"])
    document = json.loads(stdout.getvalue())
    validate_document(document, COMMAND_RESULT)
    assert code == document["exit_code"]
    return document, stderr.getvalue()


class CommandResultTests(unittest.TestCase):
    def test_schema_is_advertised_and_future_versions_are_rejected(self):
        document, stderr = call(check.main, ["--print-contract-versions"])
        self.assertEqual(document["status"], "success")
        self.assertEqual(document["operation"], "print-contract-versions")
        self.assertEqual(document["data"], manifest())
        self.assertIn("command_result", document["data"]["contracts"])
        self.assertIn("narwhal.contract-manifest", stderr)
        with self.assertRaises(ContractVersionError):
            validate_document({**document, "schema_version": 100}, COMMAND_RESULT)

    def test_each_parser_failure_produces_one_result(self):
        for command, arguments in (
            (check.main, ["--unknown-option"]),
            (probe.main, []),
            (launch_engine.main, ["start"]),
            (dev.main, ["dev", "up", "--unknown-option"]),
        ):
            with self.subTest(command=command.__module__):
                document, stderr = call(command, arguments)
                self.assertEqual(document["status"], "invalid_input")
                self.assertEqual(document["exit_code"], 2)
                self.assertEqual(document["errors"][0]["code"], "invalid_arguments")
                self.assertIn("usage:", stderr)
                self.assertEqual(document["errors"][0]["stage"], "arguments")
                self.assertTrue(document["errors"][0]["field"].startswith("--"))
                self.assertNotEqual(
                    document["errors"][0]["message"], "Command arguments failed validation"
                )
                self.assertNotIn("Traceback", stderr)

    def test_missing_inputs_keep_their_code_and_operation(self):
        with tempfile.TemporaryDirectory() as folder:
            missing = str(Path(folder) / "missing")
            for command, arguments in (
                (check.main, ["--fleet", missing]),
                (probe.main, ["--fleet", missing]),
                (launch_engine.main, ["check", "--run", missing]),
                (dev.main, ["dev", "status", "--instance", missing]),
            ):
                with self.subTest(command=command.__module__):
                    document, stderr = call(command, arguments)
                    self.assertEqual(document["status"], "invalid_input")
                    self.assertEqual(document["errors"][0]["code"], "input_missing")
                    self.assertNotIn("Traceback", stderr)

    def test_dev_default_payload_and_json_degraded_exit(self):
        payload = {"status": "degraded", "problems": ["e0 process identity expired"]}
        with patch.object(dev.lifecycle, "status", return_value=payload):
            document, _ = call(dev.main, ["dev", "status"])
            self.assertEqual(document["status"], "degraded")
            self.assertEqual(document["exit_code"], 3)
            self.assertEqual(document["data"]["problems"], payload["problems"])
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(dev.main(["dev", "status"]), 1)
            self.assertEqual(json.loads(stdout.getvalue()), payload)

    def test_engine_preparation_reports_created_plan(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _, environment = launcher_inputs(root)
            run = root / "launch"
            with patch.dict(os.environ, environment):
                document, stderr = call(launch_engine.main, ["prepare", "--out", str(run)])
            self.assertEqual(document["status"], "success")
            self.assertEqual(document["operation"], "prepare")
            plan = next(row for row in document["artifacts"] if row["kind"] == "launch")
            self.assertEqual(plan["state"], "created")
            self.assertTrue(Path(plan["path"]).is_file())
            self.assertIn("Prepared", stderr)

    def test_partial_profile_failure_reports_created_artifact_and_error(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = FleetConfig.load(ROOT / "tests/data/fleet.json")
            cfg.profiles_path = Path(folder) / "profiles.json"

            async def fail(cfg, *args, **kwargs):
                cfg.profiles_path.write_text('{"partial": true}')
                print("e0 measured; e1 failed")
                raise RuntimeError("e1 stream ended early")

            with (
                patch.object(probe.FleetConfig, "load", return_value=cfg),
                patch.object(probe, "run", side_effect=fail),
            ):
                document, stderr = call(
                    probe.main, ["--fleet", str(ROOT / "tests/data/fleet.json")]
                )
            self.assertEqual(document["status"], "error")
            self.assertEqual(document["exit_code"], 4)
            self.assertEqual(document["errors"][0]["code"], "operation_failed")
            states = {row["kind"]: row["state"] for row in document["artifacts"]}
            self.assertEqual(states, {"profiles": "created", "profile_samples": "missing"})
            self.assertIn("e1 failed", stderr)

    def test_preflight_failed_and_skipped_gates_have_distinct_statuses(self):
        cfg = FleetConfig.load(ROOT / "tests/data/fleet.json")
        for failed in (False, True):

            def profile_gate(cfg, report, failed=failed):
                if failed:
                    report.fail("profile mismatch")
                return object()

            with (
                patch.object(check.FleetConfig, "load", return_value=cfg),
                patch.object(check, "gate_reach", AsyncMock(return_value={"e0", "e1"})),
                patch.object(check, "gate_contract", AsyncMock(return_value=set())),
                patch.object(check, "gate_profile", side_effect=profile_gate),
                patch.object(check, "gate_profile_generation", AsyncMock(return_value=set())),
                patch.object(check, "gate_model", AsyncMock(return_value=set())),
                patch.object(check, "gate_pace", AsyncMock(return_value=set())),
                patch.object(check, "gate_tokenize", AsyncMock()),
                patch.object(check, "gate_slo"),
            ):
                document, _ = call(
                    check.main, ["--fleet", str(ROOT / "tests/data/fleet.json"), "--no-kv"]
                )
            self.assertEqual(document["status"], "failed_gate" if failed else "degraded")
            self.assertEqual(document["exit_code"], 1 if failed else 3)
            self.assertEqual(bool(document["data"]["failed"]), failed)
            self.assertTrue(document["data"]["skipped"])

    def test_credentials_are_redacted_from_results_and_diagnostics(self):
        secret = 'test-secret-"quoted"'

        def operation(argv):
            print(f"Failed: {secret}")
            results.set_data({"endpoint": "https://user:pass@example.org?token=querysecret"})
            raise RuntimeError("authorization Bearer bearer-secret; " + secret)

        with patch.dict(os.environ, {"NARWHAL_ENGINE_API_KEY": secret}):
            document, stderr = call(lambda argv: results.invoke("test", argv, operation), [])
        rendered = json.dumps(document) + stderr
        for value in (secret, "user:pass", "querysecret", "bearer-secret"):
            self.assertNotIn(value, rendered)
        self.assertIn("REDACTED", rendered)

    def test_redaction_preserves_wire_identifiers_and_nested_contracts(self):
        from narwhal.contracts import EFFECTIVE_CONFIG, versioned

        def operation(argv):
            results.set_data(
                versioned(
                    EFFECTIVE_CONFIG,
                    {
                        "scope": "fleet_file",
                        "source_sha256": "a" * 64,
                        "diagnostic": "credential a",
                        "credential": "a",
                    },
                )
            )
            results.record_error(
                "stage_cancelled", "credential a", stage="stage a", engine="a", field="a"
            )
            results.set_status("interrupted")
            return 130

        with patch.dict(os.environ, {"NARWHAL_ENGINE_API_KEY": "a"}):
            document, _ = call(lambda argv: results.invoke("narwhal", argv, operation), [])
            inspection, _ = call(
                dev.main, ["config", "inspect", "--fleet", str(ROOT / "tests/data/fleet.json")]
            )
        self.assertEqual(document["command"], "narwhal")
        self.assertEqual(document["status"], "interrupted")
        self.assertEqual(document["operation"], "run")
        validate_document(document["data"], EFFECTIVE_CONFIG)
        self.assertEqual(document["data"]["source_sha256"], "a" * 64)
        self.assertEqual(document["data"]["scope"], "fleet_file")
        self.assertEqual(document["data"]["credential"], "[REDACTED]")
        self.assertEqual(document["data"]["diagnostic"], "credential [REDACTED]")
        self.assertEqual(document["errors"][0]["code"], "stage_cancelled")
        self.assertEqual(document["errors"][0]["stage"], "stage a")
        self.assertEqual(document["errors"][0]["engine"], "a")
        self.assertEqual(document["errors"][0]["field"], "a")
        validate_document(inspection["data"], EFFECTIVE_CONFIG)
        self.assertEqual(inspection["command"], "narwhal")
        self.assertEqual(inspection["operation"], "config inspect")
        self.assertEqual(inspection["status"], "success")

    def test_custom_credential_environment_names_are_redacted(self):
        cfg = FleetConfig.load(ROOT / "tests/data/fleet.json")
        cfg.engine_api_key_env = "FLEET_AUTH"
        with (
            patch.dict(os.environ, {"FLEET_AUTH": "custom-credential"}),
            patch.object(probe.FleetConfig, "load", return_value=cfg),
            patch.object(probe, "run", AsyncMock(side_effect=RuntimeError("custom-credential"))),
        ):
            document, stderr = call(probe.main, ["--fleet", str(ROOT / "tests/data/fleet.json")])
        self.assertNotIn("custom-credential", json.dumps(document) + stderr)

    def test_inherited_subprocess_stdout_becomes_redacted_diagnostics(self):
        def operation(argv):
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import sys; print('child diagnostic synthetic-secret'); "
                    "print('child stderr synthetic-secret', file=sys.stderr)",
                ],
                check=True,
            )
            return 0

        with patch.dict(os.environ, {"NARWHAL_ENGINE_API_KEY": "synthetic-secret"}):
            document, stderr = call(lambda argv: results.invoke("test", argv, operation), [])
        self.assertEqual(document["status"], "success")
        self.assertIn("child diagnostic [REDACTED]", stderr)
        self.assertIn("child stderr [REDACTED]", stderr)

    def test_typed_stage_failure_preserves_recovery_context(self):
        class StageTimeout(ValueError):
            def __init__(self, message):
                super().__init__(message)
                self.stage = "check-runtime"
                self.context = {"budget_seconds": 0.1, "survivors": [], "recovery": "retry check"}

        def operation(argv):
            raise StageTimeout("runtime check deadline elapsed")

        document, _ = call(lambda argv: results.invoke("test", argv, operation), [])
        self.assertEqual(document["exit_code"], 4)
        error = document["errors"][0]
        self.assertEqual(error["code"], "stage_timeout")
        self.assertEqual(error["stage"], "check-runtime")
        self.assertEqual(error["context"]["budget_seconds"], 0.1)

    def test_json_help_completes_one_result_and_preserves_readable_help(self):
        document, stderr = call(probe.main, ["--help"])
        self.assertEqual(document["status"], "success")
        self.assertIn("--format", stderr)
