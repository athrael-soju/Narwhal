# Gate B: Package and install the approved revision

| Command   | Action                                                        |
| --------- | ------------------------------------------------------------- |
| `prepare` | Bundles the approved commit, role files, and helper snapshots. |
| `install` | Checks the commit on each host and installs Narwhal.          |

## Build an immutable deployment package

In the management checkout:

1. Load `.env` and `config/deployment.env` from Gate A.
2. Set `NARWHAL_DEPLOYMENT_REVISION` to the full approved commit.
3. Prepare the package into a new `--out` directory:

```bash
python3 tools/deployment/deploy_hosts.py prepare --out runs/deployment-env/first-deploy
```

On success the command prints:

```text
Prepared <n> hosts in runs/deployment-env/first-deploy; private manifest and inputs ready
```

Keep the `--out` directory until the deployment finishes.

The prepared run holds:

- `.env.router`
- one `.env.engine-<n>` per engine
- the fleet configuration from discovery
- profiling limits from each engine's `--max-num-seqs`
- one selected `engine-launch.engine-<n>.json` per engine
- a mode-0600 manifest

The manifest records:

- the approved revision
- the role-to-host mapping
- the input hashes
- a unique install path under `~/Narwhal-deploy/`
- SHA-256 hashes for the source bundle, helper snapshots, and role files

Later SSH steps check the prepared directory against the manifest. Credentials come from the workstation environment.

`install` places three helper snapshots under `runs/deployment-tools/` on the engine hosts. The role environment exports the path and SHA-256 of each:

| Helper snapshot                          | Path variable                | SHA-256 variable                    |
| ---------------------------------------- | ---------------------------- | ----------------------------------- |
| `tools/deployment/fabric_budget.py`      | `NARWHAL_FABRIC_BUDGET_TOOL` | `NARWHAL_FABRIC_BUDGET_SHA256`      |
| `tools/deployment/launch_engine.py`      | `NARWHAL_ENGINE_LAUNCHER`    | `NARWHAL_ENGINE_LAUNCHER_SHA256`    |
| `tools/deployment/cache_capture_hook.py` | `NARWHAL_CACHE_CAPTURE_HOOK` | `NARWHAL_CACHE_CAPTURE_HOOK_SHA256` |

## Install one host, then fan out

Install the engine 1 host:

```bash
python3 tools/deployment/deploy_hosts.py install \
  --run runs/deployment-env/first-deploy --role engine-1
```

Narwhal installs into `.venv`. On success the installer prints `<host-id>: installation ready`. A host with router and engine roles receives both role environments in one install.

When engine 1 is ready, install the remaining hosts:

```bash
python3 tools/deployment/deploy_hosts.py install --run runs/deployment-env/first-deploy
```

`install` behaviour across hosts:

- Installs the physical hosts one at a time.
- Reuses finished installations and matching transferred files.
- Stops at the first host where the source checkout changed or a file differs.

## Recover a failed installation

| Failure                                   | Recovery                                                                                     |
| ----------------------------------------- | -------------------------------------------------------------------------------------------- |
| Transfer validation fails                 | Compare the prepared hashes with the existing remote artifact before you modify it.          |
| Revision validation fails                 | Check that the bundle and role files came from the same prepared run.                        |
| A dependency installation stops early     | Rerun the same `install --run`. A host is complete when the installed `narwhal-check --help` responds. |
| A `.install-lock` remains                 | Remove it after the installer that created it has exited.                                    |

## Open installed role shells

Open the router shell:

```bash
python3 tools/deployment/deploy_hosts.py shell \
  --run runs/deployment-env/first-deploy --role router
```

Open the engine 1 shell:

```bash
python3 tools/deployment/deploy_hosts.py shell \
  --run runs/deployment-env/first-deploy --role engine-1
```

Each shell starts in the prepared remote checkout with the role environment loaded and `.venv` active.

Continue with [Gate C: Validate and start every engine](03-Validate-Engines.md).
