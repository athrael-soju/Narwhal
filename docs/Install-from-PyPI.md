# Install Narwhal from PyPI

`narwhal-inference` installs the `narwhal` Python package and its router, profiling, preflight, and attestation commands. Use Python 3.11 or newer on Linux.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install narwhal-inference
python -m pip show narwhal-inference
narwhal-check --help
```

The version printed by `pip show` identifies the installed router. Record that version with the fleet configuration, engine image, and profiles used for a deployment. Pin the approved version when installing on another router host.

Narwhal's wheel supplies the router commands. A production fleet also needs separately provisioned vLLM engines with compatible KV transfer, engine attestation, a fleet configuration, and measured profiles. Follow [Deploy a fleet](Deploy.md) to qualify those inputs before serving requests.

The repository checkout carries the CPU engine stubs, deployment helpers, and `make` targets. Run `make setup` in that checkout to install Narwhal and its development dependencies into `.venv`.
