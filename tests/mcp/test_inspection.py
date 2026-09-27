"""Exercise permission checks, real configuration commands and evidence mapping."""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from narwhal.deployment.management_registry import ManagementRegistry
from narwhal.diagnostics.management_status import observe_router
from narwhal.mcp.adapters import ToolRegistry
from narwhal.mcp.inspection import InspectionTools
from tests.fixtures import ROOT


class InspectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fleet = self.root / "fleet.json"
        self.fleet.write_bytes((ROOT / "config/fleet.example.json").read_bytes())
        self.fleet.chmod(0o600)
        self.document = {
            "schema": "narwhal.management-registry",
            "schema_version": 1,
            "registry_id": "10000000-0000-4000-8000-000000000001",
            "state_dir": str(self.root / "state"),
            "targets": [
                {
                    "id": "staging",
                    "kind": "fleet",
                    "working_directory": str(self.root),
                    "fleet_file": str(self.fleet),
                    "instance_dir": None,
                    "artifact_root": str(self.root / "artifacts"),
                    "adapter": {"id": "ssh-v1", "settings_path": str(self.root / "unused.json")},
                    "endpoints": {"router_env": "TEST_ROUTER"},
                    "credential_env": ["TOKEN"],
                }
            ],
        }
        self.configure()

    def configure(self):
        self.registry = ManagementRegistry.model_validate_json(json.dumps(self.document))
        self.tools = InspectionTools(self.registry)
        self.dispatcher = ToolRegistry(self.tools.adapters())

    async def call(self, name, **arguments):
        return await self.dispatcher.dispatch(name, {"target_id": "staging", **arguments})

    async def test_real_offline_config_commands_and_audit(self):
        for tool in ("config_inspect", "config_validate"):
            with self.subTest(tool=tool):
                result = await self.call(tool)
                self.assertEqual(result["outcome"], "success", result)
                self.assertEqual(result["data"]["schema"], "narwhal.effective-config")
                self.assertEqual(result["data"]["source"], str(self.fleet))
                self.assertEqual(result["command_result"]["status"], "success")
        audit = next(self.registry.state_dir.glob("inspection-*.jsonl"))
        rows = [json.loads(row) for row in audit.read_text().splitlines()]
        self.assertEqual([row["outcome"] for row in rows], ["started", "success"] * 2)
        self.assertNotIn(str(self.fleet), audit.read_text())
        self.assertEqual(audit.stat().st_mode & 0o777, 0o600)

    async def test_denial_and_invalid_arguments_precede_command_or_network(self):
        with (
            patch("narwhal.mcp.inspection.run_command", new_callable=AsyncMock) as command,
            patch("narwhal.mcp.inspection.observe_router", new_callable=AsyncMock) as observe,
        ):
            for name in ("config_inspect", "fleet_status", "diagnostics_collect", "artifact_read"):
                arguments = {"target_id": "missing"}
                if name == "artifact_read":
                    arguments["artifact_id"] = "10000000-0000-4000-8000-000000000009"
                result = await self.call(name, **arguments)
                self.assertEqual(result["errors"][0]["code"], "target_not_found", result)
            invalid = await self.call("config_inspect", fleet="/etc/passwd")
            self.assertEqual(invalid["errors"][0]["code"], "invalid_arguments")
            denied = await self.call("diagnostics_collect", include_request_content=True)
            self.assertEqual(denied["errors"][0]["code"], "permission_denied", denied)
            command.assert_not_awaited()
            observe.assert_not_awaited()
        self.assertFalse((self.root / "artifacts").exists())

    async def test_bad_fleet_version_or_symlink_never_starts_command(self):
        with patch("narwhal.mcp.inspection.run_command", new_callable=AsyncMock) as command:
            document = json.loads(self.fleet.read_text())
            document["schema_version"] = 999
            self.fleet.write_text(json.dumps(document))
            result = await self.call("config_validate")
            self.assertEqual(result["errors"][0]["code"], "unsupported_contract")
            self.fleet.unlink()
            self.fleet.symlink_to(ROOT / "config/fleet.example.json")
            result = await self.call("config_validate")
            self.assertEqual(result["errors"][0]["code"], "permission_denied")
            command.assert_not_awaited()

    async def test_endpoint_unset_and_partial_observations_are_distinct(self):
        with patch.dict(os.environ, {}, clear=True):
            absent = await self.call("fleet_status")
        self.assertEqual(absent["outcome"], "error", absent)
        self.assertEqual(absent["errors"][0]["code"], "source_unavailable")

        def response(request):
            if request.url.path == "/ready":
                return httpx.Response(503, json={"ready": False, "prompt": "private-request"})
            if request.url.path == "/narwhal/state":
                return httpx.Response(200, json={"schema": "narwhal.state", "schema_version": 999})
            if request.url.path == "/narwhal/lifecycle":
                raise httpx.ConnectError("TOKEN-value", request=request)
            return httpx.Response(200, json={"status": "ok", "detail": "TOKEN-value"})

        async def observe(*args, **kwargs):
            return await observe_router(*args, **kwargs, transport=httpx.MockTransport(response))

        with (
            patch.dict(os.environ, {"TEST_ROUTER": "http://localhost", "TOKEN": "TOKEN-value"}),
            patch("narwhal.mcp.inspection.observe_router", side_effect=observe),
        ):
            result = await self.call("fleet_status")
        self.assertEqual(result["outcome"], "invalid_input", result)
        rows = result["data"]["sources"]
        self.assertEqual([row["status"] for row in rows], ["ok", "ok", "error", "unavailable"])
        self.assertEqual(rows[1]["http_status"], 503)
        self.assertFalse(rows[1]["data"]["ready"])
        self.assertNotIn("TOKEN-value", json.dumps(result))
        self.assertNotIn("private-request", json.dumps(result))
        self.assertEqual(
            {e["code"] for e in result["errors"]}, {"unsupported_contract", "source_unavailable"}
        )

    async def test_large_result_is_retained_redacted_and_read_after_restart(self):
        rows = [{"source": "/health", "status": "ok", "data": {"large": "x" * 300_000}}]
        with (
            patch.dict(os.environ, {"TEST_ROUTER": "http://localhost"}),
            patch("narwhal.mcp.inspection.observe_router", return_value=rows),
        ):
            result = await self.call("fleet_status")
        self.assertEqual(result["outcome"], "degraded", result)
        self.assertEqual(result["errors"][0]["code"], "result_too_large")
        artifact_id = result["data"]["result_artifact_id"]
        self.configure()
        text, offset = "", 0
        while True:
            page = await self.call("artifact_read", artifact_id=artifact_id, offset=offset)
            self.assertEqual(page["outcome"], "success", page)
            self.assertLessEqual(len(page["data"]["text"].encode()), 32_768)
            text += page["data"]["text"]
            offset = page["data"]["next_offset"]
            if offset is None:
                break
        original = json.loads(text)
        self.assertEqual(original["outcome"], "success")
        self.assertEqual(original["data"]["sources"], rows)
        self.assertLess(len(json.dumps(result)), 262_144)

    async def test_registered_credential_and_request_content_are_redacted_in_config(self):
        document = json.loads(self.fleet.read_text())
        document["model"] = "TOKEN-value"
        self.fleet.write_text(json.dumps(document))
        with patch.dict(os.environ, {"TOKEN": "TOKEN-value"}):
            result = await self.call("config_inspect")
        self.assertEqual(result["outcome"], "success", result)
        self.assertNotIn("TOKEN-value", json.dumps(result))
        for path in self.registry.state_dir.rglob("*"):
            if path.is_file():
                self.assertNotIn("TOKEN-value", path.read_text())

    async def test_revoked_inspection_and_execution_environment_are_denied_before_spawn(self):
        with patch("narwhal.mcp.inspection.run_command", new_callable=AsyncMock) as command:
            self.document["targets"][0]["capabilities"] = []
            self.configure()
            denied = await self.call("config_inspect")
            self.assertEqual(denied["errors"][0]["code"], "permission_denied")
            self.document["targets"][0]["capabilities"] = ["inspect"]
            self.document["targets"][0]["credential_env"] = ["LD_PRELOAD"]
            self.configure()
            denied = await self.call("config_inspect")
            self.assertEqual(denied["errors"][0]["code"], "permission_denied")
            command.assert_not_awaited()

    async def test_unsafe_audit_file_is_rejected_without_blocking_or_access(self):
        self.registry.state_dir.mkdir(mode=0o700)
        audit = self.registry.state_dir / f"inspection-{self.registry.registry_id}.jsonl"
        os.mkfifo(audit, mode=0o600)
        with patch("narwhal.mcp.inspection.run_command", new_callable=AsyncMock) as command:
            result = await self.call("config_inspect")
            self.assertEqual(result["errors"][0]["code"], "permission_denied")
            command.assert_not_awaited()

    async def test_target_list_and_invalid_cursor_match_discovery_contract(self):
        result = await self.dispatcher.dispatch("target_list", {})
        self.assertEqual(result["outcome"], "success", result)
        self.assertEqual(result["data"]["targets"][0]["id"], "staging")
        self.assertNotIn(str(self.root), json.dumps(result))
        invalid = await self.dispatcher.dispatch("target_list", {"cursor": "../../source"})
        self.assertEqual(invalid["errors"][0]["code"], "invalid_cursor")

    async def test_exchange_timeout_records_interruption(self):
        async def delayed(*args, **kwargs):
            await asyncio.sleep(5)

        with (
            patch.dict(os.environ, {"TEST_ROUTER": "http://localhost"}),
            patch("narwhal.mcp.inspection.observe_router", side_effect=delayed),
        ):
            result = await self.call("fleet_status", timeout_s=1)
        self.assertEqual(result["errors"][0]["code"], "stage_timeout")
        audit = next(self.registry.state_dir.glob("inspection-*.jsonl"))
        rows = [json.loads(row) for row in audit.read_text().splitlines()]
        self.assertEqual([row["outcome"] for row in rows], ["started", "interrupted"])
        self.assertEqual(rows[-1]["error_codes"], ["stage_cancelled"])

    async def test_malformed_engine_collection_retains_cli_input_error(self):
        document = json.loads(self.fleet.read_text())
        for engines in (None, 7, {"unexpected": "object"}):
            with self.subTest(engines=engines):
                document["engines"] = engines
                self.fleet.write_text(json.dumps(document))
                result = await self.call("config_validate")
                self.assertEqual(result["outcome"], "invalid_input", result)
                self.assertEqual(result["command_result"]["status"], "invalid_input", result)
