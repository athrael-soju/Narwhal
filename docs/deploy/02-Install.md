# Gate B: Package and install the approved revision

This gate has two steps. `prepare` bundles the approved commit together with each role's files and a few helper scripts. `install` copies that package to each host, checks the commit there, and installs Narwhal.

## Build the deployment package

With `.env` and `config/deployment.env` still loaded:

```bash
python3 tools/deployment/deploy_hosts.py prepare --out runs/deployment-env/first-deploy
```

`NARWHAL_DEPLOYMENT_REVISION` has to be the full approved commit in your management checkout. Preparation bundles that commit, then clones the bundle locally to confirm the SHA before writing the manifest.

The prepared run contains:

- `.env.router` and one `.env.engine-<n>` for each engine;
- the fleet document that discovery generated;
- profiling limits taken from each engine's `--max-num-seqs`;
- one `engine-launch.engine-<n>.json` for each engine.

It also takes snapshots of three helper scripts, which are installed under `runs/deployment-tools/` on the engine hosts:

- `tools/deployment/fabric_budget.py`
- `tools/deployment/launch_engine.py`
- `tools/deployment/cache_capture_hook.py`

The role environment exports the path and SHA-256 of each one, as `NARWHAL_FABRIC_BUDGET_TOOL` / `NARWHAL_FABRIC_BUDGET_SHA256`, `NARWHAL_ENGINE_LAUNCHER` / `NARWHAL_ENGINE_LAUNCHER_SHA256`, and `NARWHAL_CACHE_CAPTURE_HOOK` / `NARWHAL_CACHE_CAPTURE_HOOK_SHA256`. Gates C and D check the launcher and the budget tool against these hashes before running them.

Preparation also writes a manifest (mode 0600). It records the approved revision, which host each role maps to, the input hashes, a unique install path under `~/Narwhal-deploy/`, and SHA-256 hashes for the source bundle, the helper snapshots, and the role files. Every later command that takes `--run` checks the prepared directory against this manifest. Management credentials stay on your workstation and are read from its environment.

Use a new `--out` directory each time you prepare, and keep it until the deployment is finished.

## Install on one host first, then the rest

Install on engine 1's host first, so that any problem shows up on one machine instead of all of them:

```bash
python3 tools/deployment/deploy_hosts.py install \
  --run runs/deployment-env/first-deploy --role engine-1
```

The installer copies the Git bundle and role files over verified SSH, clones the bundle, checks the revision, and installs Narwhal into `.venv`. If a host carries both the router and an engine, it gets both role environments but only one installation.

Once engine 1 has installed cleanly:

```bash
python3 tools/deployment/deploy_hosts.py install --run runs/deployment-env/first-deploy
```

This works through the physical hosts one at a time. Anything already installed, and any file already transferred with the same contents, is reused. If the installer finds a file at the same path with different contents, or a source checkout that has changed, it stops on that host and leaves the remaining hosts alone.

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

Leave any existing deployments on the host untouched while you recover.

- **Transfer validation failed.** Compare the prepared hashes with whatever is already on the remote host before you change anything there.
- **Revision check failed.** Make sure the bundle and the role files came from the same prepared run.
- **Dependency install was interrupted.** Run the same `install --run` again, until it completes successfully.
- **An `.install-lock` directory is left behind.** It's in the remote deployment directory, so remove it only once you've confirmed the deployment that created it has exited.

Next: [Gate C: Validate and start every engine](03-Validate-Engines.md).
