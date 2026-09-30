# Install Narwhal from PyPI

`narwhal-inference` installs the `narwhal` Python package and six commands for local development, engine launch, attestation, profiling, preflight and routing.

On Linux with Python 3.11 or newer, create a virtual environment and install the package:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install narwhal-inference
narwhal-serve --version
narwhal --help
```

## Verify the installation

`narwhal-serve --version` prints the package name and the version installed in the shell's active Python environment:

```text
narwhal-inference <version>
```

- `narwhal --help` lists the six installed commands.
- Each command accepts `--version` before its operational arguments and exits with status 0.

For each deployment:

1. Record the version with the fleet configuration, engine image, and profiles.
2. Pin the approved version on every router host.

## Before serving requests

A production fleet needs:

- separately provisioned vLLM engines with compatible KV transfer;
- engine attestation;
- a fleet configuration;
- measured profiles.

Qualify these inputs with [Deploy a fleet](Deploy.md).

## Install from a source checkout

For the deployment helpers and development checks, run `make setup` in a repository checkout to install Narwhal and its development dependencies into `.venv`.

If the `--version` flags print `narwhal-inference unknown (distribution metadata unavailable)`, run `python -m pip install -e .` to register the version.
