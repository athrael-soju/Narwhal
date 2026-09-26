"""Check the installed MCP extra and console outside the source checkout."""

import argparse
import asyncio
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

from mcp import Client, StdioServerParameters, stdio_client

import narwhal


async def check_console(root: Path, env: dict[str, str]) -> None:
    """Start the installed console using a private registry with no adapters."""
    state = root / "management"
    state.mkdir(mode=0o700)
    registry = root / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "schema": "narwhal.management-registry",
                "schema_version": 1,
                "registry_id": str(uuid.uuid4()),
                "state_dir": str(state),
                "targets": [],
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
            assert listing.tools == [], listing


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
        asyncio.run(check_console(root, env))
        asyncio.run(check_adapter(root, source, env))
    print("Installed MCP console and adapter checks passed")


if __name__ == "__main__":
    main()
