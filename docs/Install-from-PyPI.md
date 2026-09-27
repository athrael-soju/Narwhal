# Install Narwhal from PyPI

`narwhal-inference` installs the `narwhal` Python package and commands for local development, engine launch, attestation, profiling, preflight and routing. Use Python 3.11 or newer on Linux.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install narwhal-inference
narwhal-serve --version
narwhal --help
```

`narwhal-serve --version` prints `narwhal-inference` and the distribution version from the Python environment behind the executable selected by the shell. Record that version with the fleet configuration, engine image, and profiles used for a deployment. Pin the approved version when installing on another router host.

`narwhal --help` lists the installed commands and their purposes. Each command accepts `--version` before its operational arguments and exits with status 0. Source checkouts whose environment lacks distribution metadata print `narwhal-inference unknown (distribution metadata unavailable)`; install the checkout with `python -m pip install -e .` to register its version.

Narwhal's wheel supplies the router commands. A production fleet also needs separately provisioned vLLM engines with compatible KV transfer, engine attestation, a fleet configuration, and measured profiles. Follow [Deploy a fleet](Deploy.md) to qualify those inputs before serving requests.

The repository checkout carries deployment helpers and development checks. Run `make setup` in that checkout to install Narwhal and its development dependencies into `.venv`.

## Optional MCP server (unreleased)

The MCP milestone branch adds `narwhal-mcp` and the optional `mcp` extra.
The base wheel installs the entry point; running the server requires the extra.
`narwhal-mcp --help` and `--version` work without it. The server accepts local
stdio sessions and exposes ten tools for target and configuration inspection,
router status, diagnostics, artifact reads, plan inspection, and operation
listing, inspection and cancellation. Execution adapters are not installed,
so preparation, execution and resumption tools are absent from discovery.

Use the [MCP installation and client procedure](cli/MCP.md) with a checkout or
wheel containing that unreleased work.
