# Install Narwhal from PyPI

Requirements: Linux and Python 3.11 or newer.

The `narwhal-inference` package provides the `narwhal` Python package and six commands:

| Command | Purpose |
| --- | --- |
| `narwhal` | Local development |
| `narwhal-engine` | Engine launch |
| `narwhal-serve` | Routing |
| `narwhal-attest` | Engine attestation |
| `narwhal-profile` | Profiling |
| `narwhal-check` | Preflight checks |

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install narwhal-inference
narwhal-serve --version
narwhal --help
```

`narwhal-serve --version` prints `narwhal-inference` followed by the installed version. The version is that of the environment providing `narwhal-serve` on `PATH`. Confirm it is the intended one. Every command accepts `--version` as its first argument and exits with status 0.

`narwhal --help` lists the six commands and what each one is for.

From a source checkout without package metadata, the output is:

```text
narwhal-inference unknown (distribution metadata unavailable)
```

The checkout lacks distribution metadata. Run `python -m pip install -e .` to install it.

Record the version with the fleet configuration, engine image, and profiles. Pin the same version on every router host (`pip install narwhal-inference==<version>`).

## What the package doesn't include

A production fleet also requires these components, which the package does not include:

- vLLM engines, provisioned separately and configured with compatible KV transfer
- Engine attestation
- A fleet configuration
- Measured profiles

See [Deploy a fleet](Deploy.md) before serving traffic.

A source checkout includes deployment helpers and development checks. From a source checkout, run `make setup` to install Narwhal and its development dependencies into `.venv`.
