# `narwhal-mcp`

`narwhal-mcp` starts a local Model Context Protocol (MCP) server over stdio.
This command is **unreleased work on the MCP milestone branch**. It exposes six
inspection tools: `target_list`, `config_inspect`, `config_validate`,
`fleet_status`, `diagnostics_collect` and `artifact_read`. Four more tools read
retained plans and operations or record cancellation: `plan_inspect`,
`operation_list`, `operation_inspect` and `operation_cancel`.

The operation store and coordinator are available, but the distribution has no
execution adapters. `plan_prepare`, `plan_execute` and `operation_resume` are
absent from discovery. Deployment and measurement remain planned in the
[MCP contract](../MCP-Contracts.md).

The client supplies the model and conversation, then launches the server under
your local account. The server exchanges messages through stdin and stdout.
It has no HTTP listener or model dependency.

This procedure registers the repository's example fleet and validates its
configuration offline. It needs no router, engines or GPU.

## 1. Install the server

On a Linux management workstation with Python 3.11 or newer, open a terminal
in a checkout containing the MCP server and create its environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[mcp]'
narwhal-mcp --version
narwhal-mcp --help
```

`--version` reports the installed `narwhal-inference` distribution version.
The `mcp` extra selects MCP Python SDK `>=2.2.0,<2.3`. When installing a wheel
built from this checkout, select its `mcp` extra to install the SDK.

The base installation includes the entry point and supports `--help` and
`--version` without the SDK. Starting a session without the SDK exits 2 and writes
a diagnostic to stderr directing you to install
`narwhal-inference[mcp]`.

## 2. Prepare a local registry

The server requires a `narwhal.management-registry` version 1 document owned
by its operating-system user with mode `0600`. The file must be regular,
at most 1 MiB, and its final path component must not be a symlink. Registry
paths can be relative to the server's startup directory; paths inside the
document must be absolute. Unknown fields and duplicate JSON keys are rejected.
The [registration contract](../mcp/Registration.md) defines the complete schema.

In the same terminal at the repository root, create the example's registry and
copy `config/fleet.example.json` into a private working directory. The script
creates `runs/mcp` under the ignored `runs/` location and refuses to reuse an
existing directory:

```bash
python - <<'PY'
import json
import os
from pathlib import Path
from uuid import uuid4

fleet = Path("config/fleet.example.json").read_bytes()
root = Path("runs/mcp").resolve()
root.mkdir(parents=True, mode=0o700)
for name in ("state", "artifacts"):
    (root / name).mkdir(mode=0o700)
with os.fdopen(os.open(root / "fleet.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
    stream.write(fleet)
registry = root / "registry.json"
document = {
    "schema": "narwhal.management-registry",
    "schema_version": 1,
    "registry_id": str(uuid4()),
    "state_dir": str(root / "state"),
    "targets": [
        {
            "id": "example-fleet",
            "kind": "fleet",
            "working_directory": str(root),
            "artifact_root": str(root / "artifacts"),
            "fleet_file": str(root / "fleet.json"),
            "instance_dir": None,
            "adapter": {
                "id": "ssh-v1",
                "settings_path": str(root / "site-settings.json"),
            },
            "capabilities": ["inspect"],
        }
    ],
}
with os.fdopen(os.open(registry, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
    json.dump(document, stream, indent=2)
    stream.write("\n")
print(registry)
PY
```

The fleet registry schema requires an adapter settings path. This example
reserves `site-settings.json`; the inspection tools do not read that file.
Deployment adapter settings remain part of the planned deployment workflow.
The example has only the `inspect` grant and no action grants or router endpoint.

The server requires `state_dir` and `artifact_root` to be owned by its user with
mode `0700`. The working directory, each input file and the directory containing
that file must belong to that user and deny group and other write access.
Input access rejects symlinks in every path component. `config_inspect`,
`config_validate` and `fleet_status` reject fleet files larger than 8 MiB.
`diagnostics_collect` retains at most 8 MiB of input per source and marks larger
sources `truncated`. Relative paths in the copied fleet resolve from `runs/mcp`.
Retain `registry_id` when editing a registry used for continued inspection.

## 3. Configure a client

Configure your client's local stdio launcher with the absolute path to the
virtual environment's `narwhal-mcp` executable. Pass `--registry` and the
absolute registry path printed above as separate arguments. Enter these values
using your client's executable and argument settings.

The equivalent server invocation is:

```bash
narwhal-mcp --registry runs/mcp/registry.json
```

The foreground process waits for MCP messages on stdin and writes protocol
responses to stdout. It sends diagnostics to stderr. The client stops the
process when closing the session. If you start the command manually, press
Ctrl+C to stop it.

## 4. Verify initialization and discovery

From the same repository root and activated environment, run this SDK client
to check the server independently of your agent application:

```bash
python - <<'PY'
import asyncio
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    server = StdioServerParameters(
        command="narwhal-mcp",
        args=["--registry", str(Path("runs/mcp/registry.json").resolve())],
    )
    async with stdio_client(server) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            initialized = await session.initialize()
            listed = await session.list_tools()
            print("protocol:", initialized.protocol_version)
            print("tools:", sorted(tool.name for tool in listed.tools))
            targets = await session.call_tool("target_list", {})
            assert targets.structured_content["outcome"] == "success", targets
            ids = [target["id"] for target in targets.structured_content["data"]["targets"]]
            assert ids == ["example-fleet"], ids
            print("targets:", ids)
            checked = await session.call_tool("config_validate", {"target_id": "example-fleet"})
            result = checked.structured_content
            assert result["outcome"] == "success", result
            assert result["data"]["schema"] == "narwhal.effective-config", result
            assert result["command_result"]["status"] == "success", result
            print("config_validate:", result["outcome"])

asyncio.run(main())
PY
```

A successful check prints the negotiated protocol version, the ten available
tools, `targets: ['example-fleet']` and `config_validate: success`. The client then
closes the session. This check verifies registry loading, target discovery and
a successful call to the installed offline configuration validator. The example
fleet still contains placeholder hardware and model names. Validation checks
the configuration fields and their relationships; live engine identity,
profiles and readiness require deployment qualification.

The installed wheel has been checked with MCP Python SDK 2.2.0 clients on Python
3.11, 3.12 and 3.13, using protocol versions `2025-11-25` and `2026-07-28`.
Those checks cover initialization, discovery, and valid and invalid calls to
fixture tools. The client example above negotiates `2025-11-25` with SDK 2.2.0.

## Options and failures

| Option | Default | Contract |
| --- | --- | --- |
| `--registry PATH` | `NARWHAL_MANAGEMENT_REGISTRY` | Select the local management registry. The explicit argument takes precedence; absence of both is an input error. |
| `--version` | | Print the installed distribution version and exit 0. |

`-h` and `--help` print usage and exit 0. Startup exits 2 for a missing SDK,
missing registry selection or invalid registry. Read the stderr diagnostic,
correct the dependency, path, permissions or document fields, then repeat the
SDK check. The server loads the registry on startup; restart it after editing
the registry.

A normally closed session exits 0. Ctrl+C exits 130; an unexpected server
failure exits 4 and writes a diagnostic to stderr.

Inspection writes private audit receipts and listing snapshots under
`state_dir`. The coordinator retains operation records, request deduplication
and resource reservations there, together with immutable plans and their inputs.
Diagnostic collection and oversized responses also create redacted
exports under `artifact_root`; `artifact_read` retrieves them by ID. See
[Tools and results](../mcp/Tools.md) for endpoint requirements, content policy
and collection limits.

Setting `NARWHAL_MANAGEMENT_REGISTRY` also opts supported finite CLI commands
into the [management binding](../mcp/Registration.md#registry-changes-and-retention).
Commands that change a deployment or write measurement artifacts fail with
`adapter_unavailable` until their execution adapters are installed. Configuration,
diagnostic and status reads remain available.

To remove this example after closing the session, delete its `runs/mcp`
directory, including the copied fleet, registry and inspection records. For
targets whose artifacts you need later, retain the registry, `state_dir` and
`artifact_root`.
