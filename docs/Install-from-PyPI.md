# Install Narwhal from PyPI

The `narwhal-inference` package gives you the `narwhal` Python package and six commands for local development, engine launch, attestation, profiling, preflight checks, and routing. You'll need Linux and Python 3.11 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install narwhal-inference
narwhal-serve --version
narwhal --help
```

`narwhal-serve --version` prints `narwhal-inference` followed by the installed version. That version comes from whichever Python environment your shell finds `narwhal-serve` in, so if you have more than one environment around, check it's the one you meant.

Write the version down next to the fleet configuration, engine image, and profiles you're deploying with. When you install on other router hosts, pin that exact version.

`narwhal --help` lists the six commands and what each one is for. Every command accepts `--version` as its first argument and exits with status 0.

If you're running from a source checkout, you might see this instead:

```text
narwhal-inference unknown (distribution metadata unavailable)
```

That means the environment has no package metadata for the checkout. Install it with `python -m pip install -e .` and the real version will show up.

## What the package doesn't include

The wheel only gives you the router commands. To run a production fleet you also need vLLM engines (provisioned separately, with compatible KV transfer), engine attestation, a fleet configuration, and measured profiles. [Deploy a fleet](Deploy.md) walks you through qualifying each of these before you serve any traffic.

The repository checkout also has deployment helpers and development checks. If you're working from a clone, run `make setup` to install Narwhal and its development dependencies into `.venv`.
