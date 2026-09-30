# Deployment inputs and SSH trust

## 12. Deployment inputs and generated artifacts

A fresh management checkout starts from `.env`. [Launch policy](../deploy/01-Discover.md#confirm-the-launch-policy) lists the launch-policy defaults and their environment overrides.

Discovery writes the generated private files at mode 0600. Keep these files together per fleet:

```text
config/hosts.local.json
config/ssh.known_hosts
config/engine-launch.local.json
config/engine-launch.sources.json
config/fleet.json
config/deployment.env
```

| Directory               | Contents                                                                                  |
| ----------------------- | ----------------------------------------------------------------------------------------- |
| `runs/discovery/<run>/` | Per-engine checkpoint file hashes, a matching tree digest, observations, and command logs |
| `runs/deployment-env/`  | Prepared workstation files, Git-ignored                                                   |
| `runs/deployment/`      | Remote role environments and the effective fleet                                          |

Load `.env` and `config/deployment.env` before reusing saved discovery inputs.

[Inspect each engine host](../deploy/03-Validate-Engines.md#inspect-every-engine-host) against its generated launch record.

### 12.1 Source revision and deployment bundle

`NARWHAL_DEPLOYMENT_REVISION` is the full commit SHA from the management checkout.

Prepare deployment inputs for [host installation](../deploy/02-Install.md):

```bash
python3 tools/deployment/deploy_hosts.py prepare --out <directory>
```

| Command                                | Output                                                                                                                                    |
| -------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| `prepare`                              | `source.bundle` with the selected revision, verified with a fresh local clone and written next to the role environments in `runs/deployment-env/` |
| `tools/deployment/prepare_host_env.py` | Role files at mode 0600, with shell literals copied through unchanged                                                                     |
| `deploy_hosts.py install`              | The bundle on each selected host, with each remote checkout cloned from it and verified against the role file's revision before installation |

### 12.2 Generated host-side files

| Remote file                                   | Contents                                                                                                                                                                | Source                                                                                                                                                                                                   |
| --------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `runs/deployment/.env.router`                 | Revision, `NARWHAL_FLEET=runs/deployment/fleet.json`, referenced engine and attestation URLs, configured engine credential, and optional router and monitoring stack settings. | `NARWHAL_DEPLOYMENT_REVISION`, variables referenced by fleet endpoint fields and `engine.engine_api_key_env`, `NARWHAL_ROUTER_URL`, `NARWHAL_GRAFANA_BIND_ADDRESS`, `NARWHAL_PROMETHEUS_LISTEN_ADDRESS`. |
| `runs/deployment/.env.engine-<n>`             | Revision, launch and artifact fields, selected node URLs, fabric peer addresses, and configured engine credential.                                                      | Shared engine fields, optional `NARWHAL_NODE_<n>_<field>` overrides, derived service URLs and fabric addresses, and configured engine credential.                                                        |
| `config/engine-launch.engine-<n>.json`        | GPU allocation, TP size, device mappings, resolved UCX selection, and generated launch arguments.                                                                       | The engine role in workstation `NARWHAL_LAUNCH_CONFIG`.                                                                                                                                                  |
| `runs/deployment-tools/launch_engine.py`      | Standalone launcher snapshot, with its path and SHA-256 in `.env.engine-<n>`.                                                                                           | Management-checkout `tools/deployment/launch_engine.py`.                                                                                                                                                 |
| `runs/deployment-tools/fabric_budget.py`      | Standalone fabric calculator snapshot, with its path and SHA-256 in `.env.engine-<n>`.                                                                                  | Management-checkout `tools/deployment/fabric_budget.py`.                                                                                                                                                 |
| `runs/deployment-tools/cache_capture_hook.py` | Serving cache-capture snapshot, with its path and SHA-256 in `.env.engine-<n>`.                                                                                         | Management-checkout `tools/deployment/cache_capture_hook.py`.                                                                                                                                            |
| `runs/deployment/fleet.json`                  | Effective generated fleet copied before deployment edits.                                                                                                               | Workstation file selected by `NARWHAL_FLEET`.                                                                                                                                                            |

Engine export requires:

```text
NARWHAL_ENGINE_IMAGE
NARWHAL_ENGINE_MODEL_NAME
NARWHAL_MODEL_DIR
NARWHAL_RUN_DIR
NARWHAL_MODEL_CONFIG_SHA256
NARWHAL_FABRIC_INTERFACE
NARWHAL_ENGINE_PORT
NARWHAL_ATTEST_PORT
NARWHAL_NIXL_SIDE_CHANNEL_PORT
NARWHAL_UCX_TCP_PORT_RANGE
```

The exporter also sets `NARWHAL_ENGINE_LAUNCH_CONFIG=config/engine-launch.engine-<n>.json`.

Per-node overrides insert `NODE_<n>_` after `NARWHAL_`. For example, `NARWHAL_NODE_2_ENGINE_PORT` becomes `NARWHAL_ENGINE_PORT` inside `.env.engine-2`.

Each role file gets its role's fields plus the variables named in fleet endpoint references and `engine.engine_api_key_env`.

Export fails when:

- a required field's override is empty
- a variable name contains `SSH` or matches an access variable from the host inventory

### 12.3 Profiling limits

Deployment preparation writes `profiling-limits.json` beside the router's effective fleet. Each engine limit comes from the generated launch record's `--max-num-seqs`.

Profile with the generated limits:

```bash
narwhal-profile --limits runs/deployment/profiling-limits.json
```

### 12.4 Role shells

Load a generated role environment:

```bash
deploy_hosts.py shell --run <directory> --role <role>
```

The command turns off shell tracing while loading the role file.

A host with both roles holds both role files in one checkout. Each role shell loads its own file.

---

## 13. Host inventory and SSH trust

`NARWHAL_HOSTS` selects the physical-host inventory. Default: `config/hosts.local.json`.

Discovery groups roles with identical `NARWHAL_NODE_<n>_SSH` or `NARWHAL_ROUTER_SSH` destinations into one physical-host record.

Each `hosts` entry contains:

- unique `id`
- `ssh_env`, naming the environment variable holding the management destination
- optional `password_env`
- assigned `roles`

The `router` role appears once. Each engine role is named `engine-<n>`, where `<n>` matches the numbered deployment variables.

The [example inventory](https://github.com/athrael-soju/Narwhal/blob/main/config/hosts.example.json) assigns `router` and `engine-1` to one machine and `engine-2` to another.

| Value                              | Location           |
| ---------------------------------- | ------------------ |
| Destination values and credentials | Workstation `.env` |
| Variable names                     | Inventory          |

SSH destinations may be aliases or `user@management-host`.

| `password_env` | Authentication                                                     |
| -------------- | ------------------------------------------------------------------ |
| Omitted        | OpenSSH key or agent authentication                                |
| Set            | Password authentication from the named variable, which must be populated |

### 13.1 Inventory validation

Validate the inventory:

```bash
tools/deployment/deploy_hosts.py plan
```

| Command               | Action                                                                                          |
| --------------------- | ----------------------------------------------------------------------------------------------- |
| `plan`                | Checks for unique host IDs, one host per role, distinct destination entries, and set access variables |
| `check-access`        | Verifies one connection per host                                                                |
| `shell --role <role>` | Resolves the role's host from the inventory                                                     |

### 13.2 Known-hosts handling

SSH host keys establish server identity. `NARWHAL_SSH_KNOWN_HOSTS` selects the SSH known-hosts file. Discovery uses `config/ssh.known_hosts` when the variable is unset.

| Command             | Host-key checking                          |
| ------------------- | ------------------------------------------ |
| Discovery           | OpenSSH `accept-new`                       |
| Deployment commands | Strict host-key checking against this file |

Under `accept-new`:

- the first connection records the host key
- OpenSSH rejects a changed key
- existing verified stores keep prior entries

The workstation needs OpenSSH, plus `sshpass` for password authentication.

For key or agent authentication:

1. Put the address, username, identity, and optional `Port` or `ProxyJump` configuration under an alias in the workstation's private SSH config.
2. Point `ssh_env` at that alias.

To replace the key of a changed server:

1. Verify the replacement through the supplied private access source.
2. Update the checkout-local known-hosts file.

### 13.3 Prepared and remote runs

`prepare --out <directory>` writes:

- one verified source bundle
- role files grouped by host ID
- a private manifest containing revision, host assignments, destination fingerprints, and file hashes

Use a new output directory for each deployment.

`install --run <directory>` checks the manifest against current host mappings and local files. It transfers and installs once per selected host.

Host selection:

- `--role engine-1` selects the host that runs `engine-1`, including colocated roles.
- `--host <id>` selects one physical host directly.

Re-running install reuses existing content when the completion marker matches the approved revision.

Each remote run lives under `~/Narwhal-deploy/<id>/`, which contains:

- source bundle
- role files
- `checkout/`
- installation lock
- completion marker

A source or configuration mismatch stops the helper at that host.

Private local logs under the run's `logs/` directory retain executed scripts, exit status, and host output.

A role shell opened with `--run` starts with:

- the deployed checkout as its working directory
- the matching role environment loaded
- the checkout's virtual environment active
