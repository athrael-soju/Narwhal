# Gate B: Package and install the approved revision

`prepare` bundles the approved commit, the role files and three helper scripts. `install` copies the package to each host, verifies the commit and installs Narwhal.

## Build the deployment package

With `.env` and `config/deployment.env` still loaded:

```bash
python3 tools/deployment/deploy_hosts.py prepare --out runs/deployment-env/first-deploy
```

Set `NARWHAL_DEPLOYMENT_REVISION` to the full approved commit SHA from your management checkout. `prepare` bundles that commit, clones the bundle locally to confirm the SHA, then writes the manifest.

The prepared run directory contains:

- `.env.router` and one `.env.engine-<n>` for each engine
- the fleet document that discovery generated
- profiling limits taken from each engine's `--max-num-seqs`
- one `engine-launch.engine-<n>.json` for each engine

`prepare` also copies three helper scripts, which are installed under `runs/deployment-tools/` on the engine hosts:

- `tools/deployment/fabric_budget.py`
- `tools/deployment/launch_engine.py`
- `tools/deployment/cache_capture_hook.py`

The role environment exports the path and SHA-256 of each script:

| Script | Path variable | Hash variable |
| --- | --- | --- |
| `fabric_budget.py` | `NARWHAL_FABRIC_BUDGET_TOOL` | `NARWHAL_FABRIC_BUDGET_SHA256` |
| `launch_engine.py` | `NARWHAL_ENGINE_LAUNCHER` | `NARWHAL_ENGINE_LAUNCHER_SHA256` |
| `cache_capture_hook.py` | `NARWHAL_CACHE_CAPTURE_HOOK` | `NARWHAL_CACHE_CAPTURE_HOOK_SHA256` |

Gates C and D check the launcher and the budget tool against these hashes before running them.

`prepare` also writes a manifest (mode 0600). It records the approved revision, which host each role maps to, the input hashes, a unique install path under `~/Narwhal-deploy/`, and SHA-256 hashes for the source bundle, the helper scripts and the role files. Every later command that takes `--run` checks the prepared run directory against this manifest.

Management credentials stay on your workstation and are read from its environment.

Use a new `--out` directory for each `prepare` run and keep it until the deployment finishes.

## Install on one host first, then the rest

Install on engine 1's host first to limit any problem to one machine:

```bash
python3 tools/deployment/deploy_hosts.py install \
  --run runs/deployment-env/first-deploy --role engine-1
```

The installer copies the Git bundle and role files over verified SSH, clones the bundle, checks the revision, and installs Narwhal into `.venv`. If a host carries both the router and an engine, it gets both role environments and one shared installation.

When engine 1 has installed cleanly, install on the remaining hosts:

```bash
python3 tools/deployment/deploy_hosts.py install --run runs/deployment-env/first-deploy
```

The installer processes physical hosts one at a time and reuses anything already installed or already transferred with identical contents. If a file at the same path has different contents or a source checkout has changed, it stops and leaves the remaining hosts untouched.

## Open role shells

From your management terminals:

```bash
python3 tools/deployment/deploy_hosts.py shell \
  --run runs/deployment-env/first-deploy --role router
```

```bash
python3 tools/deployment/deploy_hosts.py shell \
  --run runs/deployment-env/first-deploy --role engine-1
```

Each shell opens in the prepared remote checkout with the role environment loaded and `.venv` active.

## If installation fails

Leave existing deployments on the host untouched during recovery.

- **Transfer validation failed.** Compare the manifest hashes with the files on the remote host before changing anything there.
- **Revision check failed.** Make sure the bundle and the role files came from the same prepared run.
- **Dependency install was interrupted.** Rerun the same `install --run` command until it completes.
- **An `.install-lock` directory is left behind.** The directory is in the remote deployment directory. Remove it only after confirming the install process that created it has exited.

Next: [Gate C: Validate and start every engine](03-Validate-Engines.md).
