---
description: Package a checked Narwhal wheel with each host's role files and install it on the router and engine hosts.
---

# Install Narwhal on the hosts

Run these commands from the management checkout after discovery. `narwhal deploy package` writes the package that `narwhal deploy install` copies to each host.

## Building the deployment package

Each host installs Narwhal from one release wheel. Download it from PyPI, or build it with `python -m build --wheel` in a clone at its release tag `v<version>`:

```bash
python -m pip download --no-deps --only-binary :all: --dest dist 'narwhal-inference==<version>'
sha256sum dist/narwhal_inference-<version>-py3-none-any.whl
```

`package` refuses a wheel whose version is not `<major>.<minor>.<patch>`, because the router host's tools checkout is the matching release tag. Keep the wheel's SHA-256, and the commit that the tag names, in the private record.

Compile the hash-pinned lock of the wheel's dependencies, as [Dependency lock](../configuration/04-Deployment-Inputs.md#dependency-lock) describes, and keep its SHA-256 with the wheel's.

In the management checkout:

1. Load `.env` and `config/deployment.env` from deployment discovery.
2. Set `NARWHAL_WHEEL` to the path of the wheel, such as `dist/narwhal_inference-<version>-py3-none-any.whl`.
3. Set `NARWHAL_WHEEL_SHA256` to the wheel's SHA-256 from the private record.
4. Set `NARWHAL_REQUIREMENTS` to the dependency lock and `NARWHAL_REQUIREMENTS_SHA256` to its SHA-256.
5. Set `NARWHAL_TOOLS_COMMIT` to the full commit of the tag `v<version>`.
6. Set management credentials in the workstation environment.
7. Build the package into a new `--out` directory:

    ```bash
    narwhal deploy package --out runs/deployment-env/first-deploy
    ```

`package` refuses a wheel or lock whose SHA-256 differs from its input. On success it prints:

```text
Packaged <n> hosts in runs/deployment-env/first-deploy; private manifest and inputs ready
```

Keep the `--out` directory until the deployment finishes.

The package holds these files at mode 0600:

| Path | Content |
| --- | --- |
| `narwhal_inference-<version>-py3-none-any.whl` | A copy of the wheel that `NARWHAL_WHEEL` names |
| `requirements.txt` | A copy of the dependency lock that `NARWHAL_REQUIREMENTS` names |
| `<host-id>/.env.<role>` | One role environment per role on the host |
| `<host-id>/engine-launch.engine-<n>.json` | The selected launch record for each engine role |
| `<host-id>/fleet.local.json` | The fleet configuration from discovery, on the router host |
| `<host-id>/profiling-limits.json` | Profiling limits from each engine's sequence limit, on the router host |
| `<host-id>/fabric_budget.py`, `cache_capture_hook.py`, `transfer_measure.py` | Helper snapshots for engine hosts |
| `manifest.json` | The private manifest |

The manifest records:

- the wheel's file name and SHA-256, the lock's SHA-256, and `NARWHAL_TOOLS_COMMIT`
- the role-to-host mapping
- SHA-256 hashes of the SSH destinations
- a unique deployment directory under `~/Narwhal-deploy/`
- SHA-256 hashes for the wheel, the lock, the helper snapshots, and the role files

Each role environment sets `NARWHAL_DEPLOYMENT_REVISION` to the wheel's SHA-256, so every launch plan and attestation records the wheel that the host runs.

Engine hosts receive these helper snapshots under `runs/deployment-tools/`. Each engine's role environment holds the path and the SHA-256 of every snapshot:

| Helper snapshot | Delivered for | Path variable | SHA-256 variable |
| --- | --- | --- | --- |
| `fabric_budget.py`, from `narwhal.deployment` | Every fleet | `NARWHAL_FABRIC_BUDGET_TOOL` | `NARWHAL_FABRIC_BUDGET_SHA256` |
| `cache_capture_hook.py`, from `narwhal.backends.<backend>` | vLLM, the default, and TensorRT-LLM | `NARWHAL_CACHE_CAPTURE_HOOK` | `NARWHAL_CACHE_CAPTURE_HOOK_SHA256` |
| `transfer_measure.py`, from `narwhal.backends.sglang` | SGLang | `NARWHAL_TRANSFER_MEASURE_TOOL` | `NARWHAL_TRANSFER_MEASURE_SHA256` |

`package` takes the snapshots from the wheel, so engine hosts run the helpers of the release they install. The snapshots run with the engine host's `python3` and the standard library only. Check a snapshot's SHA-256 before running it, as [Qualify the transfer fabric](04-Qualify-Fabric.md) does.

## Installing engine 1 and the remaining hosts

Each host needs Python 3.11 or later with `venv`, coreutils, and access to a Python package index for the locked dependencies.

1. Install the engine 1 host:

    ```bash
    narwhal deploy install --run runs/deployment-env/first-deploy --role engine-1
    ```

2. Confirm that the installer prints `<host-id>: installation ready`.
3. Install the remaining hosts:

    ```bash
    narwhal deploy install --run runs/deployment-env/first-deploy
    ```

On each host, `install` copies the package into `~/Narwhal-deploy/<id>/package/`, checking each file's SHA-256 after the transfer. It then checks the SHA-256 of the wheel and the lock again, creates `.venv` in the deployment directory `~/Narwhal-deploy/<id>/`, and installs them into it:

```bash
.venv/bin/python -m pip install --no-deps --require-hashes --only-binary :all: -r package/requirements.txt
.venv/bin/python -m pip install --no-deps --no-index package/narwhal_inference-<version>-py3-none-any.whl
.venv/bin/python -m pip check
```

It checks that the installed `narwhal-inference` version matches the wheel, and copies each role file to its working path in the deployment directory.

`install` reuses a finished installation or a matching transferred file. It stops at a host with a transferred file that differs from the manifest, a working file that differs from the package, or an installation of another wheel. On a host with router and engine roles, it installs every role environment together.

## Recovering a failed installation

If transfer validation fails, compare the packaged hashes with the existing remote file before modifying it.

If the installed version differs from the wheel, check that `NARWHAL_WHEEL` named the intended release, then build a new package.

If `pip` cannot find a locked file for the host's platform or Python version, or `pip check` reports a missing dependency, compile the lock again as [Dependency lock](../configuration/04-Deployment-Inputs.md#dependency-lock) describes and build a new package.

If a dependency installation stops early, rerun the same `install --run` until the installed `narwhal-check --help` responds.

An installer that was killed can leave `.install-lock` in the deployment directory. `install` then stops with `<path>/.install-lock exists, taken by process <pid> on <host> at <time>`. Check that the process has exited, then remove the directory with `rm -r` and rerun `install`.

## Opening installed role shells

Open the router shell:

```bash
narwhal deploy shell --run runs/deployment-env/first-deploy --role router
```

Open the engine 1 shell:

```bash
narwhal deploy shell --run runs/deployment-env/first-deploy --role engine-1
```

Each shell opens with:

- the deployment directory `~/Narwhal-deploy/<id>/` as working directory
- the role environment loaded
- `.venv` active

## Installing the repository tools on the router host

The wheel holds the router, the engine launcher and the checks. The monitoring stack that `make observe` starts and the [fleet control service](../operate/06-Controlling-the-Fleet.md) live in the repository's `tools/` directory, which the wheel leaves out. Run them from a clone of the repository at the release of the installed wheel, with the deployment's virtual environment.

In the router shell:

1. Clone the repository at the wheel's release tag into a new directory beside the deployment directory, replacing `<version>` with the wheel's version:

    ```bash
    git clone --depth 1 --branch v<version> https://github.com/athrael-soju/Narwhal.git ../tools-<version>
    ```

2. Check that the clone is at the commit that the package recorded. The router environment holds it as `NARWHAL_TOOLS_COMMIT`:

    ```bash
    test "$(git -C ../tools-<version> rev-parse HEAD)" = "$NARWHAL_TOOLS_COMMIT"
    ```

    If the check fails, remove the clone and stop. The tag differs from the commit in the private record.

3. Link the deployment's virtual environment into the clone, so its commands run on the installed wheel:

    ```bash
    ln -s "$VIRTUAL_ENV" ../tools-<version>/.venv
    ```

Later pages call this clone the tools checkout. Run `make observe` and the fleet control service from it, in the router shell.

[Validate and start engines](03-Validate-Engines.md)
