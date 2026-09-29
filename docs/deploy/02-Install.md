# Gate B: Package and install the approved revision

`prepare` bundles the approved commit, role files, and helper snapshots. `install` checks the commit on each host, then installs Narwhal.

## Build an immutable deployment package

From the management checkout, with `.env` and `config/deployment.env` loaded from Gate A, set `NARWHAL_DEPLOYMENT_REVISION` to the full approved commit. Preparation clones the bundle locally and checks the SHA before writing the manifest.

Use a new `--out` directory for each preparation. You need it until the deployment finishes.

Prepare the package:

```bash
python3 tools/deployment/deploy_hosts.py prepare --out runs/deployment-env/first-deploy
```

On success the command prints:

```text
Prepared <n> hosts in runs/deployment-env/first-deploy; private manifest and inputs ready
```

The prepared run holds `.env.router` and one `.env.engine-<n>` per engine. It also holds the fleet configuration from discovery, profiling limits taken from each engine's `--max-num-seqs`, and one selected `engine-launch.engine-<n>.json` per engine.

`install` also places three helper snapshots under `runs/deployment-tools/` on the engine hosts. The role environment exports each path and its SHA-256:

- `tools/deployment/fabric_budget.py` through `NARWHAL_FABRIC_BUDGET_TOOL` and `NARWHAL_FABRIC_BUDGET_SHA256`;
- `tools/deployment/launch_engine.py` through `NARWHAL_ENGINE_LAUNCHER` and `NARWHAL_ENGINE_LAUNCHER_SHA256`;
- `tools/deployment/cache_capture_hook.py` through `NARWHAL_CACHE_CAPTURE_HOOK` and `NARWHAL_CACHE_CAPTURE_HOOK_SHA256`.

`prepare` writes a mode-0600 manifest. It records the approved revision, the role-to-host mapping, the input hashes, a unique install path under `~/Narwhal-deploy/`, and SHA-256 hashes for the source bundle, helper snapshots, and role files. Later SSH steps check the prepared directory against it. Credentials come from the workstation environment.

## Install one host, then fan out

Install the engine 1 host first:

```bash
python3 tools/deployment/deploy_hosts.py install \
  --run runs/deployment-env/first-deploy --role engine-1
```

The installer copies the Git bundle and role files over verified SSH, clones the bundle, checks the approved revision, and installs Narwhal into `.venv`. It prints `<host-id>: installation ready` when done. A host with both router and engine roles gets both environments in one install.

Once engine 1 is ready, install the remaining hosts:

```bash
python3 tools/deployment/deploy_hosts.py install --run runs/deployment-env/first-deploy
```

`install` goes through the physical hosts one at a time and reuses finished installations and matching transferred files. It stops at the first host where the source checkout changed or a file differs, and it leaves later hosts alone.

## Recover a failed installation

- Transfer validation fails: compare the prepared hashes with the existing remote artifact before you modify it.
- Revision validation fails: check that the bundle and role files came from the same prepared run.
- A dependency installation stops early: rerun the same `install --run`. A host counts as complete once the installed `narwhal-check --help` responds.
- A `.install-lock` remains: remove it after the installer that created it has exited.

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
