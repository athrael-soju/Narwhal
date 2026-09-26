# `narwhal-mcp`

`narwhal-mcp` starts a local Model Context Protocol (MCP) server over stdio.
This command is **unreleased work on the MCP milestone branch**. The server
validates a management registry, accepts client initialization and returns an
empty tool list. Fleet inspection, deployment and recovery tools are planned
in the [MCP contract](../MCP-Contracts.md).

The client supplies the model and conversation, then launches the server under
your local account. The server exchanges messages through stdin and stdout.
It has no HTTP listener or model dependency.

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

In the same terminal at the repository root, create an empty registry for the
protocol check below. This script creates private directories under the ignored
`runs/` location and refuses to overwrite an existing registry:

```bash
python - <<'PY'
import json
import os
from pathlib import Path
from uuid import uuid4

root = Path("runs/mcp").resolve()
root.mkdir(parents=True, exist_ok=True, mode=0o700)
root.chmod(0o700)
state_dir = root / "state"
state_dir.mkdir(exist_ok=True, mode=0o700)
state_dir.chmod(0o700)
registry = root / "registry.json"
document = {
    "schema": "narwhal.management-registry",
    "schema_version": 1,
    "registry_id": str(uuid4()),
    "state_dir": str(state_dir),
    "targets": [],
}
with os.fdopen(os.open(registry, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
    json.dump(document, stream, indent=2)
    stream.write("\n")
print(registry)
PY
```

Retain `registry_id` when editing the document. This empty registry is sufficient
to verify the current server's initialization and discovery behaviour.

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
            print("tools:", [tool.name for tool in listed.tools])

asyncio.run(main())
PY
```

A successful check prints the negotiated protocol version and `tools: []`.
The client then closes the session. This check establishes that the installed
server can initialize and answer discovery requests. It does not check a GPU
or fleet.

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

To remove the example after closing the session, delete `runs/mcp/registry.json`
and the empty `runs/mcp/state` and `runs/mcp` directories. Keep a registry used
for later operation adapters with its retained management state.
