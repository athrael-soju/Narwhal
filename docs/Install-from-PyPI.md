# Install Narwhal from PyPI

`narwhal-inference` installs the `narwhal` Python package and six commands for local development, engine launch, attestation, profiling, preflight and routing.

Requirements are Linux and Python 3.11 or newer.

Install the package in a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install narwhal-inference
narwhal-serve --version
narwhal --help
```

## Verify the installation

Expected `narwhal-serve --version` output:

```text
narwhal-inference <version>
```

- `narwhal --help` lists the six installed commands.
- Each command accepts `--version` before its operational arguments.
- Each `--version` invocation exits with status 0.

For each deployment:

1. Record the version with the fleet configuration, engine image, and profiles.
2. Pin the approved version on every router host.

## Before serving requests

A production fleet needs these inputs, qualified in [Deploy a fleet](Deploy.md):

- separately provisioned vLLM engines with compatible KV transfer
- engine attestation
- a fleet configuration
- measured profiles

## Install from a source checkout

In a repository checkout, `make setup` installs Narwhal and its development dependencies into `.venv`.

If the `--version` flags print `narwhal-inference unknown (distribution metadata unavailable)`, run `python -m pip install -e .`.
