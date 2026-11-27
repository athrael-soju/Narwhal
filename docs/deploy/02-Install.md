---
description: Package a checked Narwhal wheel with each host's role files and install it on the router and engine hosts.
---

# Install Narwhal on the hosts

The second gate runs two stages. `package` checks the release wheel and its dependency lock against their SHA-256 values and writes the package: the wheel, the lock, each host's role files and a private manifest. `install` copies the package to every host and installs it into a virtual environment in the host's deployment directory. Narwhal installs nothing else on a host: Python, Docker, systemd and the GPU tools are the site's, as [Getting started](Getting-Started.md#preparing-ssh-hosts) lists.

## Building the deployment package

Each host installs Narwhal from one release wheel. Download it from PyPI, or build it with `python -m build --wheel` in a clone at its release tag `v<version>`:

```bash
python -m pip download --no-deps --only-binary :all: --dest dist 'narwhal-inference==<version>'
sha256sum dist/narwhal_inference-<version>-py3-none-any.whl
```

`package` refuses a wheel whose version is not `<major>.<minor>.<patch>`. Compile the hash-pinned lock of the wheel's dependencies, as [Dependency lock](../configuration/04-Deployment-Inputs.md#dependency-lock) describes, and keep both SHA-256 values in the private record. Then set the four inputs:

| Variable | Value |
| --- | --- |
| `NARWHAL_WHEEL` | Path of the wheel, such as `dist/narwhal_inference-<version>-py3-none-any.whl` |
| `NARWHAL_WHEEL_SHA256` | The wheel's SHA-256 |
| `NARWHAL_REQUIREMENTS` | Path of the dependency lock |
| `NARWHAL_REQUIREMENTS_SHA256` | The lock's SHA-256 |

These four are the run's `revision` input. A new release changes them, and the next `run` repeats `package` and every stage after it, as [Upgrade and rollback](../operate/04-Upgrade-and-Validate.md) describes. `package` runs once on the workstation after `discover`, from the configuration discovery wrote, and writes the package to `stages/package/attempt-<n>/package/`. To run it alone, with `.env` and `config/deployment.env` loaded:

```bash
narwhal deploy package --out runs/deployment-env/first-deploy
```

`package` refuses a wheel or lock whose SHA-256 differs from its input. It also refuses a fleet whose `hardware.accelerator`, `hardware.accelerators_per_engine` or `hardware.tensor_parallel` differs from an engine's launch record `accelerator`, number of `gpu_ids` or `tensor_parallel_size`, naming the role and the field. [Inspecting every engine host](03-Validate-Engines.md#inspecting-every-engine-host) then checks each launch record against its host's GPUs. Alone, it prints `Packaged <n> hosts in runs/deployment-env/first-deploy; private manifest and inputs ready` on success.

The package holds these files at mode 0600:

| Path | Content |
| --- | --- |
| `narwhal_inference-<version>-py3-none-any.whl` | A copy of the wheel that `NARWHAL_WHEEL` names |
| `requirements.txt` | A copy of the dependency lock that `NARWHAL_REQUIREMENTS` names |
| `<host-id>/.env.<role>` | One role environment per role on the host |
| `<host-id>/engine-launch.engine-<n>.json` | The selected launch record for each engine role |
| `<host-id>/fleet.local.json` | The fleet configuration from discovery, on the router host |
| `<host-id>/profiling-limits.json` | Profiling limits from each engine's sequence limit, on the router host |
| `<host-id>/cache_capture_hook.py`, `transfer_measure.py` | Helper snapshots for engine hosts of a backend that uses them |
| `manifest.json` | The private manifest |

The manifest records:

- the wheel's file name and SHA-256, and the lock's SHA-256
- the role-to-host mapping
- SHA-256 hashes of the SSH destinations
- a unique deployment directory under `~/Narwhal-deploy/`
- SHA-256 hashes for the wheel, the lock, the helper snapshots, and the role files

Each role environment sets `NARWHAL_DEPLOYMENT_REVISION` to the wheel's SHA-256, so every launch plan and attestation records the wheel that the host runs.

Engine hosts of a backend that captures its cache layout or measures its handoffs receive a helper snapshot under `runs/deployment-tools/`. Each engine's role environment holds the path and the SHA-256 of every snapshot:

| Helper snapshot | Delivered for | Path variable | SHA-256 variable |
| --- | --- | --- | --- |
| `cache_capture_hook.py`, from `narwhal.backends.<backend>` | vLLM, the default, and TensorRT-LLM | `NARWHAL_CACHE_CAPTURE_HOOK` | `NARWHAL_CACHE_CAPTURE_HOOK_SHA256` |
| `transfer_measure.py`, from `narwhal.backends.sglang` | SGLang | `NARWHAL_TRANSFER_MEASURE_TOOL` | `NARWHAL_TRANSFER_MEASURE_SHA256` |

`package` takes the snapshots from the wheel, so engine hosts run the helpers of the release they install. The snapshots run with the engine host's `python3` and the standard library only. `narwhal-engine prepare` and `narwhal deploy fabric` check a snapshot against its SHA-256 before they use it.

## Installing Narwhal on each host

`install` runs once for each host of the inventory after `package`. Each host needs Python 3.11 or later with `venv`, coreutils, and access to a Python package index for the locked dependencies.

On each host, `install` copies the package into `~/Narwhal-deploy/<id>/package/`, checking each file's SHA-256 after the transfer. It then checks the SHA-256 of the wheel and the lock again, creates `.venv` in the deployment directory `~/Narwhal-deploy/<id>/`, and installs them into it:

```bash
.venv/bin/python -m pip install --no-deps --require-hashes --only-binary :all: -r package/requirements.txt
.venv/bin/python -m pip install --no-deps --no-index package/narwhal_inference-<version>-py3-none-any.whl
.venv/bin/python -m pip check
```

It checks that the installed `narwhal-inference` version matches the wheel, and copies each role file to its working path in the deployment directory. On a host with router and engine roles, it installs every role environment together. Each host's log is in `stages/install/<host-id>/attempt-<n>/logs/`.

`install` reuses a finished installation or a matching transferred file, and a run whose inputs did not change skips the stage, so it neither repackages nor reinstalls. It stops at a host with a transferred file that differs from the manifest, a working file that differs from the package, or an installation of another wheel.

To run it alone, for every host or for the hosts that `--host` or `--role` select:

```bash
narwhal deploy install --run runs/deployment-env/first-deploy
narwhal deploy install --run runs/deployment-env/first-deploy --role engine-1
```

Each host prints `<host-id>: installation ready`.

## Recovering a failed installation

If transfer validation fails, compare the packaged hashes with the existing remote file before modifying it.

If the installed version differs from the wheel, check that `NARWHAL_WHEEL` named the intended release.

If `pip` cannot find a locked file for the host's platform or Python version, or `pip check` reports a missing dependency, compile the lock again as [Dependency lock](../configuration/04-Deployment-Inputs.md#dependency-lock) describes and set its new SHA-256. The next `run` packages again.

If a dependency installation stops early, run `narwhal deploy run` again. `install` resumes on that host.

An installer that was killed can leave `.install-lock` in the deployment directory. `install` then stops with `<path>/.install-lock exists, taken by process <pid> on <host> at <time>`. Check that the process has exited, then remove the directory with `rm -r` and run again.

## Opening installed role shells

`narwhal deploy shell` opens a login shell on a role's host in the deployment directory, with the role environment loaded and `.venv` active. The commands that run a stage alone run there. For a host that `narwhal deploy run` installed, pass the inventory and the package from the run directory:

```bash
narwhal deploy shell --role engine-1 \
  --hosts runs/deploy/first-deploy/stages/discover/attempt-1/config/hosts.local.json \
  --run runs/deploy/first-deploy/stages/package/attempt-1/package
```

Use the attempt numbers of the last passed `discover` and `package`, which `narwhal deploy status` prints. For a package built alone, pass its directory to `--run` and leave out `--hosts`. The router shell takes `--role router`.

Each stage of the run works in `runs/deploy/<run-id>/stages/<stage>/<instance>/attempt-<n>/` under the deployment directory, so an engine's launch directory is `runs/deploy/<run-id>/stages/engine-prepare/engine-<n>/attempt-<k>/launch`. The run ID is in the run record.

The wheel also holds the monitoring stack and the [fleet control service](../operate/06-Controlling-the-Fleet.md), so the router shell runs them from `.venv` with no repository checkout.

[Validate and start engines](03-Validate-Engines.md)
