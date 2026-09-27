"""Check the installed MCP extra and console outside the source checkout."""

import argparse
import asyncio
import json
import os
import sys
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mcp import Client, StdioServerParameters, stdio_client

import narwhal


async def check_console(root: Path, source: Path, env: dict[str, str]) -> None:
    """Start the installed console using a private registry and real offline configuration tools."""
    state = root / "management"
    state.mkdir(mode=0o700)
    fleet = root / "fleet.json"
    fleet.write_bytes((source / "config/fleet.example.json").read_bytes())
    fleet.chmod(0o600)
    registry = root / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "schema": "narwhal.management-registry",
                "schema_version": 1,
                "registry_id": str(uuid.uuid4()),
                "state_dir": str(state),
                "targets": [
                    {
                        "id": "installed",
                        "kind": "fleet",
                        "working_directory": str(root),
                        "artifact_root": str(root / "artifacts"),
                        "fleet_file": str(fleet),
                        "instance_dir": None,
                        "endpoints": {"router_env": "MCP_CHECK_ROUTER"},
                        "credential_env": ["MCP_CHECK_SECRET"],
                        "adapter": {"id": "ssh-v1", "settings_path": str(root / "site.json")},
                    }
                ],
            }
        )
    )
    registry.chmod(0o600)
    parameters = StdioServerParameters(
        command="narwhal-mcp", args=["--registry", str(registry)], cwd=root, env=env
    )
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr:
        async with Client(
            stdio_client(parameters, errlog=stderr), mode="legacy", read_timeout_seconds=5
        ) as client:
            listing = await client.list_tools()
            assert {tool.name for tool in listing.tools} == {
                "target_list",
                "config_inspect",
                "config_validate",
                "fleet_status",
                "diagnostics_collect",
                "artifact_read",
                "operation_list",
                "operation_inspect",
                "operation_cancel",
                "plan_inspect",
            }, listing
            targets = await client.call_tool("target_list", {})
            assert targets.structured_content["data"]["targets"][0]["id"] == "installed", targets
            operations = await client.call_tool("operation_list", {"target_id": "installed"})
            assert operations.structured_content["data"] == {
                "operations": [],
                "next_cursor": None,
            }, operations
            for name in ("config_inspect", "config_validate"):
                checked = await client.call_tool(name, {"target_id": "installed"})
                assert not checked.is_error, checked
                document = checked.structured_content
                assert document["outcome"] == "success", document
                assert document["data"]["schema"] == "narwhal.effective-config", document
                assert document["data"]["source"] == str(fleet), document
            status = await client.call_tool("fleet_status", {"target_id": "installed"})
            assert status.structured_content["outcome"] == "success", status
            collected = await client.call_tool("diagnostics_collect", {"target_id": "installed"})
            assert collected.structured_content["outcome"] == "success", collected
            artifact = collected.structured_content["data"]["manifest_artifact_id"]
            retained = await client.call_tool(
                "artifact_read",
                {"target_id": "installed", "artifact_id": artifact},
            )
            assert retained.structured_content["outcome"] == "success", retained
            assert "narwhal.diagnostic-bundle" in retained.structured_content["data"]["text"]
            for path in (root / "artifacts").rglob("*"):
                if path.is_file():
                    payload = path.read_bytes()
                    assert b"installed-secret-value" not in payload, path
                    assert b"installed-request-content" not in payload, path
            rejected = await client.call_tool("config_inspect", {"target_id": "unregistered"})
            assert rejected.is_error, rejected
            assert rejected.structured_content["errors"][0]["code"] == "target_not_found", rejected


async def check_adapter(root: Path, source: Path, env: dict[str, str]) -> None:
    """Exercise a synthetic adapter while importing only installed server code."""
    fixture = source / "tests/mcp/fixture_server.py"
    assert fixture.is_file(), fixture
    parameters = StdioServerParameters(
        command=sys.executable, args=[str(fixture)], cwd=root, env=env
    )
    for mode in ("legacy", "2026-07-28"):
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr:
            async with Client(
                stdio_client(parameters, errlog=stderr), mode=mode, read_timeout_seconds=5
            ) as client:
                listing = await client.list_tools()
                assert [tool.name for tool in listing.tools] == ["fixture_echo"], listing
                valid = await client.call_tool(
                    "fixture_echo", {"target_id": "installed", "mode": "noisy"}
                )
                assert not valid.is_error, valid
                document = valid.structured_content
                assert document["schema"] == "narwhal.management-result", document
                assert document["schema_version"] == 1, document
                assert document["data"] == {"count": 1, "calls": 1}, document
                assert len(valid.content) == 1, valid
                assert json.loads(valid.content[0].text) == document, valid
                invalid = await client.call_tool(
                    "fixture_echo", {"target_id": "installed", "count": True}
                )
                assert invalid.is_error, invalid
                assert invalid.structured_content["outcome"] == "invalid_input", invalid
                later = await client.call_tool("fixture_echo", {"target_id": "installed"})
                assert later.structured_content["data"]["calls"] == 2, later
            stderr.seek(0)
            diagnostics = stderr.read()
            for marker in (
                "fixture-python-stdout",
                "fixture-logging-stderr",
                "fixture-raw-fd-stdout",
                "fixture-child-stdout",
            ):
                assert marker in diagnostics, diagnostics


class RouterFixture(BaseHTTPRequestHandler):
    """Return synthetic CPU observations to the installed inspection tools."""

    def log_message(self, *args):
        """Keep fixture HTTP diagnostics out of the test output."""

    def do_GET(self):
        """Supply each documented router route without running an inference service."""
        document = {
            "status": "ok",
            "detail": "installed-secret-value",
            "prompt": "installed-request-content",
        }
        if self.path in {"/narwhal/state", "/narwhal/lifecycle"}:
            document.update(schema=self.path.replace("/narwhal/", "narwhal."), schema_version=1)
        payload = json.dumps(document).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main() -> None:
    """Verify that the installed distribution supplies MCP protocol behaviour."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    arguments = parser.parse_args()
    source = arguments.source_root.resolve()
    installed = Path(narwhal.__file__).resolve()
    assert not installed.is_relative_to(source), f"Narwhal imported from checkout: {installed}"
    assert not Path.cwd().resolve().is_relative_to(source), "Run outside the checkout"
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("NARWHAL_MANAGEMENT_REGISTRY", None)
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        with ThreadingHTTPServer(("127.0.0.1", 0), RouterFixture) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            env.update(
                MCP_CHECK_ROUTER=f"http://127.0.0.1:{server.server_port}",
                MCP_CHECK_SECRET="installed-secret-value",
            )
            try:
                asyncio.run(check_console(root, source, env))
                asyncio.run(check_adapter(root, source, env))
            finally:
                server.shutdown()
                worker.join(timeout=2)
    print("Installed MCP console and adapter checks passed")


if __name__ == "__main__":
    main()
