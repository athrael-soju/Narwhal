# Install Narwhal from PyPI

`narwhal-inference` installs the `narwhal` Python package and six commands for local development, engine launch, attestation, profiling, preflight and routing. Use Python 3.11 or newer on Linux.

Create a virtual environment and install the package:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install narwhal-inference
narwhal-serve --version
narwhal --help
```

## Verify the installation

`narwhal-serve --version` prints the package name and the distribution version in use:

```text
narwhal-inference <version>
```

That version comes from the Python environment the shell selects for the `narwhal-serve` executable. Record it with the fleet configuration, engine image, and profiles of the deployment, and pin the approved version on every router host.

`narwhal --help` lists the six installed commands. Each one accepts `--version` before its operational arguments and exits with status 0.

## Before serving requests

A production fleet also needs:

- separately provisioned vLLM engines with compatible KV transfer;
- engine attestation;
- a fleet configuration;
- measured profiles.

Follow [Deploy a fleet](Deploy.md) to qualify these inputs before serving requests.

## Install from a source checkout

The repository checkout adds deployment helpers and development checks. Run `make setup` in the checkout to install Narwhal and its development dependencies into `.venv`.

If the checkout environment lacks distribution metadata, the `--version` flags print `narwhal-inference unknown (distribution metadata unavailable)`. Run `python -m pip install -e .` to register the version.
