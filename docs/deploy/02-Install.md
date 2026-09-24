# Gate B: Package and install the approved revision

`prepare` bundles the approved commit with role-specific files and helper snapshots; `install` verifies that commit on each host before installing Narwhal.

## Build an immutable deployment package

With `.env` and `config/deployment.env` still loaded:

```bash
python3 tools/deployment/deploy_hosts.py prepare --out runs/deployment-env/first-deploy
```

`NARWHAL_DEPLOYMENT_REVISION` must be the full approved commit in the management checkout. Preparation bundles that commit and verifies the exact SHA by cloning the bundle locally before finalising the manifest.

The prepared run contains `.env.router`, one `.env.engine-<n>` per engine, the discovery-generated fleet document, profiling limits derived from each engine's `--max-num-seqs`, and one selected `engine-launch.engine-<n>.json` per engine.

Preparation also snapshots:

- `tools/deployment/fabric_budget.py`;
- `tools/deployment/launch_engine.py`;
- `tools/deployment/cache_capture_hook.py`.

These checkout paths resolve to the same source files packaged under `narwhal.deployment`. The multi-host installer snapshots their bytes and hashes as before; an installed workstation workflow imports the package directly.

These are installed under `runs/deployment-tools/` on engine hosts. The role environment exports each path and SHA-256 through `NARWHAL_FABRIC_BUDGET_TOOL` / `NARWHAL_FABRIC_BUDGET_SHA256`, `NARWHAL_ENGINE_LAUNCHER` / `NARWHAL_ENGINE_LAUNCHER_SHA256`, and `NARWHAL_CACHE_CAPTURE_HOOK` / `NARWHAL_CACHE_CAPTURE_HOOK_SHA256`.

Preparation writes a mode-0600 manifest with the approved revision, role-to-host mapping, input hashes, a unique install path under `~/Narwhal-deploy/`, and SHA-256 hashes of the source bundle, helper snapshots, and role files. Later SSH operations revalidate the prepared directory against this manifest and read management credentials from the workstation environment.

Use a new `--out` directory for every preparation and retain it through deployment.

## Install one host, then fan out

Prove installation on the host carrying engine 1:

```bash
python3 tools/deployment/deploy_hosts.py install \
  --run runs/deployment-env/first-deploy --role engine-1
```

The installer transfers the Git bundle and role files over verified SSH, clones the bundle, checks the approved revision, and installs Narwhal into `.venv`. A host carrying router and engine roles receives both environments but only one installation.

After engine 1 succeeds:

```bash
python3 tools/deployment/deploy_hosts.py install --run runs/deployment-env/first-deploy
```

The helper iterates physical hosts. Completed installations and matching transferred files are reused. A same-path file with different contents or a changed source checkout stops that host before later hosts are touched.

## Open installed role shells

Open installed role shells from management terminals:

```bash
python3 tools/deployment/deploy_hosts.py shell \
  --run runs/deployment-env/first-deploy --role router
```

```bash
python3 tools/deployment/deploy_hosts.py shell \
  --run runs/deployment-env/first-deploy --role engine-1
```

Each shell starts in the prepared remote checkout, loads the role environment, and activates `.venv`.

Recovery rules:

- Transfer validation failure: compare prepared hashes with the existing remote artifact before touching it.
- Revision validation failure: verify the bundle and role files came from the same prepared run.
- Interrupted dependency installation: rerun the same `install --run`; completion is marked only after the CLI responds.
- `.install-lock`: remove only after proving the owning installer has exited.
- Preserve existing deployments during recovery.

Continue with [Gate C: Prove each host and one engine per cache class](03-Validate-Engines.md).
