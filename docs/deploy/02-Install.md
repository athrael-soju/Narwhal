# Gate B: Package and install the approved revision

| Command   | Action                                                         |
| --------- | -------------------------------------------------------------- |
| `prepare` | Bundles the approved commit, role files, and helper snapshots. |
| `install` | Installs the approved commit on each host.                     |

## Build an immutable deployment package

In the management checkout:

1. Load `.env` and `config/deployment.env` from Gate A.
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

Prepared run, files at mode 0600:

| Path                                                                      | Content                                                                  |
| ------------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| `source.bundle`                                                           | Git bundle of the approved commit                                        |
| `<host-id>/.env.<role>`                                                   | One role environment per role on the host                                |
| `<host-id>/engine-launch.engine-<n>.json`                                 | The selected launch record for each engine role                          |
| `<host-id>/fleet.local.json`                                              | The fleet configuration from discovery, on the router host               |
| `<host-id>/profiling-limits.json`                                         | Profiling limits from each engine's `--max-num-seqs`, on the router host |
| `<host-id>/fabric_budget.py`, `launch_engine.py`, `cache_capture_hook.py` | Helper snapshots, on engine hosts                                        |
| `manifest.json`                                                           | The private manifest                                                     |

The manifest records:

- the approved revision
- the role-to-host mapping
- SHA-256 hashes of the SSH destinations
- a unique install path under `~/Narwhal-deploy/`
- SHA-256 hashes for the source bundle, helper snapshots, and role files

Helper snapshots under `runs/deployment-tools/` on the engine hosts:

| Helper snapshot                          | Path variable                | SHA-256 variable                    |
| ---------------------------------------- | ---------------------------- | ----------------------------------- |
| `tools/deployment/fabric_budget.py`      | `NARWHAL_FABRIC_BUDGET_TOOL` | `NARWHAL_FABRIC_BUDGET_SHA256`      |
| `tools/deployment/launch_engine.py`      | `NARWHAL_ENGINE_LAUNCHER`    | `NARWHAL_ENGINE_LAUNCHER_SHA256`    |
| `tools/deployment/cache_capture_hook.py` | `NARWHAL_CACHE_CAPTURE_HOOK` | `NARWHAL_CACHE_CAPTURE_HOOK_SHA256` |

## Install engine 1 and the remaining hosts

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

`install` behaviour on each host:

| Host state                                         | `install` action                          |
| -------------------------------------------------- | ----------------------------------------- |
| Finished installation or matching transferred file | Reuses it.                                |
| Changed source checkout                            | Stops at that host.                       |
| Differing file                                     | Stops at that host.                       |
| Router and engine roles on one host                | Installs every role environment together. |

## Recover a failed installation

| Failure                               | Recovery                                                                            |
| ------------------------------------- | ----------------------------------------------------------------------------------- |
| Transfer validation fails             | Compare the prepared hashes with the existing remote artifact before modifying it.  |
| Revision validation fails             | Check that the bundle and role files came from the same prepared run.               |
| A dependency installation stops early | Rerun the same `install --run` until the installed `narwhal-check --help` responds. |
| A `.install-lock` remains             | Remove it after the installer that created it has exited.                           |

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

Each shell opens with:

- the prepared remote checkout as working directory
- the role environment loaded
- `.venv` active

[![Next: Gate C: Validate and start every engine](https://img.shields.io/badge/next-Gate%20C%3A%20Validate%20and%20start%20every%20engine-0f766e)](03-Validate-Engines.md)
