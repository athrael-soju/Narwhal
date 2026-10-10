---
description: Package an approved Narwhal revision and install it on the router and engine hosts.
---

# Install the approved revision

`prepare` bundles the approved commit, role files, and helper snapshots. `install` installs the approved commit on each host.

## Building an immutable deployment package

In the management checkout:

1. Load `.env` and `config/deployment.env` from deployment discovery.
2. Set `NARWHAL_DEPLOYMENT_REVISION` to the full approved commit.
3. Set management credentials in the workstation environment.
4. Prepare the package into a new `--out` directory:

    ```bash
    python3 tools/deployment/deploy_hosts.py prepare --out runs/deployment-env/first-deploy
    ```

On success the command prints:

```text
Prepared <n> hosts in runs/deployment-env/first-deploy; private manifest and inputs ready
```

Keep the `--out` directory until the deployment finishes.

The prepared run holds these files at mode 0600:

| Path                                                                      | Content                                                                  |
| ------------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| `source.bundle`                                                           | Git bundle of the approved commit                                        |
| `<host-id>/.env.<role>`                                                   | One role environment per role on the host                                |
| `<host-id>/engine-launch.engine-<n>.json`                                 | The selected launch record for each engine role                          |
| `<host-id>/fleet.local.json`                                              | The fleet configuration from discovery, on the router host               |
| `<host-id>/profiling-limits.json`                                         | Profiling limits from each engine's sequence limit, on the router host   |
| `<host-id>/fabric_budget.py`, `cache_capture_hook.py`                     | Helper snapshots, on engine hosts                                        |
| `manifest.json`                                                           | The private manifest                                                     |

The manifest records:

- the approved revision
- the role-to-host mapping
- SHA-256 hashes of the SSH destinations
- a unique install path under `~/Narwhal-deploy/`
- SHA-256 hashes for the source bundle, helper snapshots, and role files

Helper snapshots under `runs/deployment-tools/` on the engine hosts:

| Helper snapshot                                        | Path variable                | SHA-256 variable                    |
| ------------------------------------------------------ | ---------------------------- | ----------------------------------- |
| `tools/deployment/fabric_budget.py`                    | `NARWHAL_FABRIC_BUDGET_TOOL` | `NARWHAL_FABRIC_BUDGET_SHA256`      |
| `src/narwhal/backends/<backend>/cache_capture_hook.py` | `NARWHAL_CACHE_CAPTURE_HOOK` | `NARWHAL_CACHE_CAPTURE_HOOK_SHA256` |

`<backend>` is the fleet's `engine.backend`.

## Installing engine 1 and the remaining hosts

1. Install the engine 1 host:

    ```bash
    python3 tools/deployment/deploy_hosts.py install \
      --run runs/deployment-env/first-deploy --role engine-1
    ```

2. Confirm that the installer prints `<host-id>: installation ready`.
3. Install the remaining hosts:

    ```bash
    python3 tools/deployment/deploy_hosts.py install --run runs/deployment-env/first-deploy
    ```

`install` reuses a finished installation or a matching transferred file. It stops at a host with a changed source checkout or a differing file. On a host with router and engine roles, it installs every role environment together.

## Recovering a failed installation

If transfer validation fails, compare the prepared hashes with the existing remote artifact before modifying it.

If revision validation fails, check that the bundle and role files came from the same prepared run.

If a dependency installation stops early, rerun the same `install --run` until the installed `narwhal-check --help` responds.

If a `.install-lock` remains, remove it after the installer that created it has exited.

## Opening installed role shells

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

Each shell opens with:

- the prepared remote checkout as working directory
- the role environment loaded
- `.venv` active

[Validate and start engines](03-Validate-Engines.md)
