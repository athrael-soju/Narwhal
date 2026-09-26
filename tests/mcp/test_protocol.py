"""Exercise SDK clients and raw stdio framing against a separate server process."""

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

FIXTURE = Path(__file__).with_name("fixture_server.py")
HAS_MCP = importlib.util.find_spec("mcp") is not None


def child_environment():
    """Keep the installed import path and remove caller-selected management state."""
    env = os.environ.copy()
    env.pop("NARWHAL_MANAGEMENT_REGISTRY", None)
    return env


def envelope(response):
    """Require one equivalent text and structured result from the protocol adapter."""
    document = response.structured_content
    assert isinstance(document, dict), response
    assert document["schema"] == "narwhal.management-result", document
    assert document["schema_version"] == 1, document
    assert len(response.content) == 1, response.content
    assert response.content[0].type == "text", response.content
    assert json.loads(response.content[0].text) == document, response
    return document


@unittest.skipUnless(HAS_MCP, "requires the optional mcp extra")
class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_and_modern_sdk_clients_validate_without_invoking_bad_calls(self):
        from mcp import Client, MCPError, StdioServerParameters, stdio_client

        for mode, version in (("legacy", "2025-11-25"), ("2026-07-28", "2026-07-28")):
            with (
                self.subTest(mode=mode),
                tempfile.TemporaryDirectory() as folder,
                tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr,
            ):
                params = StdioServerParameters(
                    command=sys.executable,
                    args=[str(FIXTURE)],
                    cwd=folder,
                    env=child_environment(),
                )
                async with Client(
                    stdio_client(params, errlog=stderr),
                    mode=mode,
                    read_timeout_seconds=5,
                ) as client:
                    self.assertEqual(client.protocol_version, version)
                    listing = await client.list_tools()
                    self.assertEqual([tool.name for tool in listing.tools], ["fixture_echo"])
                    tool = listing.tools[0]
                    self.assertFalse(tool.input_schema["additionalProperties"])
                    self.assertIn("target_id", tool.input_schema["required"])
                    self.assertEqual(tool.input_schema["properties"]["timeout_s"]["maximum"], 30)
                    self.assertEqual(tool.output_schema["type"], "object")

                    good = await client.call_tool("fixture_echo", {"target_id": "trial"})
                    self.assertFalse(good.is_error)
                    self.assertEqual(envelope(good)["data"], {"count": 1, "calls": 1})

                    for arguments in (
                        {},
                        {"target_id": "trial", "unknown": "field"},
                        {"target_id": "trial", "count": True},
                        {"target_id": "trial", "count": "2"},
                        {"target_id": "trial", "count": 11},
                        {"target_id": "trial", "timeout_s": True},
                        {"target_id": "trial", "timeout_s": 31},
                    ):
                        with self.subTest(arguments=arguments):
                            bad = await client.call_tool("fixture_echo", arguments)
                            self.assertTrue(bad.is_error)
                            document = envelope(bad)
                            self.assertEqual(document["outcome"], "invalid_input")
                            self.assertTrue(document["errors"])

                    final = await client.call_tool(
                        "fixture_echo", {"target_id": "trial", "count": 2, "mode": "noisy"}
                    )
                    self.assertEqual(envelope(final)["data"], {"count": 2, "calls": 2})
                    gate = await client.call_tool(
                        "fixture_echo", {"target_id": "trial", "mode": "failed_gate"}
                    )
                    self.assertTrue(gate.is_error)
                    self.assertEqual(envelope(gate)["outcome"], "failed_gate")
                    self.assertEqual(envelope(gate)["errors"][0]["code"], "gate_failed")

                    with self.assertRaises(MCPError) as caught:
                        await client.call_tool("missing_tool", {})
                    self.assertEqual(caught.exception.code, -32602)
                stderr.seek(0)
                diagnostic_output = stderr.read()
                for marker in (
                    "Narwhal MCP server started",
                    "Narwhal MCP server stopped",
                    "fixture-python-stdout",
                    "fixture-logging-stderr",
                    "fixture-raw-fd-stdout",
                    "fixture-child-stdout",
                ):
                    self.assertIn(marker, diagnostic_output)

    async def test_protocol_errors_and_clean_eof_preserve_stdout_framing(self):
        with tempfile.TemporaryDirectory() as folder:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(FIXTURE),
                cwd=folder,
                env=child_environment(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            async def send(document):
                process.stdin.write(json.dumps(document).encode() + b"\n")
                await process.stdin.drain()

            async def response(request_id):
                line = await asyncio.wait_for(process.stdout.readline(), timeout=5)
                self.assertTrue(line, "server closed stdout before its response")
                document = json.loads(line)
                self.assertEqual(document["jsonrpc"], "2.0")
                self.assertEqual(document["id"], request_id)
                return document

            try:
                await send(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-11-25",
                            "capabilities": {},
                            "clientInfo": {"name": "narwhal-wire-test", "version": "1"},
                        },
                    }
                )
                initialized = await response(1)
                self.assertEqual(initialized["result"]["protocolVersion"], "2025-11-25")
                await send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                await send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {}})
                invalid = await response(2)
                self.assertEqual(invalid["error"]["code"], -32602)
                await send(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "fixture_echo",
                            "arguments": {"target_id": "trial", "mode": "noisy"},
                        },
                    }
                )
                called = await response(3)
                self.assertFalse(called["result"]["isError"])
                self.assertEqual(called["result"]["structuredContent"]["data"]["calls"], 1)
                process.stdin.close()
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
                self.assertEqual(process.returncode, 0, stderr.decode())
                self.assertEqual(stdout, b"", "stdout must contain only requested protocol frames")
                diagnostic_output = stderr.decode()
                for marker in (
                    "Narwhal MCP server started",
                    "Narwhal MCP server stopped",
                    "fixture-python-stdout",
                    "fixture-logging-stderr",
                    "fixture-raw-fd-stdout",
                    "fixture-child-stdout",
                ):
                    self.assertIn(marker, diagnostic_output)
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()


if __name__ == "__main__":
    unittest.main()
