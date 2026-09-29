# Deployment inputs and SSH trust

## 12. Deployment inputs and generated artifacts

Discovery reads `.env`, inspects each host, and writes the host inventory, SSH trust store, fleet, launch records, and source index.

See [Launch policy](../deploy/01-Discover.md#confirm-the-launch-policy) for defaults and environment overrides.

A fresh management checkout starts from `.env`. Discovery writes the generated private files with mode 0600.

Keep these files together per fleet:

```text
config/hosts.local.json
config/ssh.known_hosts
config/engine-launch.local.json
config/engine-launch.sources.json
config/fleet.json
config/deployment.env
```

`runs/discovery/<run>/` holds per-engine checkpoint file hashes, a matching tree digest, observations, and command logs.

Load `.env` and `config/deployment.env` before reusing saved discovery inputs.

Engine inspection is covered in [Inspect every engine host](../deploy/03-Validate-Engines.md#inspect-every-engine-host).

Prepared workstation files go to the Git-ignored `runs/deployment-env/` directory. Remote role environments and the effective fleet go to `runs/deployment/`.

### 12.1 Source revision and deployment bundle

`NARWHAL_DEPLOYMENT_REVISION` is the full commit SHA from the management checkout.

Prepare deployment inputs:

```bash
python3 tools/deployment/deploy_hosts.py prepare --out <directory>
```

`prepare` packages the selected revision as `source.bundle` and verifies it with a fresh local clone.

`deploy_hosts.py install` copies that bundle to each selected host. Each remote checkout clones from it and verifies its revision against the role file before installation.

`prepare` writes `source.bundle` next to the role environments in `runs/deployment-env/`.

`tools/deployment/prepare_host_env.py` writes the role files at mode 0600 and copies shell literals through unchanged.

See [Host installation](../deploy/02-Install.md) for the sequence.

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

Export fails when a required field's override is empty.

Each role file gets its role's fields. Variables named in fleet endpoint references and `engine.engine_api_key_env` are added.

Export refuses variables named like SSH access variables, including any name containing `SSH` or matching an access variable from the host inventory.

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

A host with both roles keeps both files in one checkout, and each role shell loads only its own.

---

## 13. Host inventory and SSH trust

`NARWHAL_HOSTS` selects the physical-host inventory. Default: `config/hosts.local.json`.

Discovery groups roles with identical `NARWHAL_NODE_<n>_SSH` or `NARWHAL_ROUTER_SSH` destinations into one physical-host record.

Each `hosts` entry contains:

- unique `id`
- `ssh_env`, naming the environment variable holding the management destination
- optional `password_env`
- assigned `roles`

The `router` role appears once. Engine roles use the name pattern `engine-<n>` and map to the numbered deployment variables.

The [example inventory](https://github.com/athrael-soju/Narwhal/blob/main/config/hosts.example.json) assigns `router` and `engine-1` to one machine and `engine-2` to another.

Keep destination values and credentials in workstation `.env`; the inventory stores variable names only.

SSH destinations may be aliases or `user@management-host`.

OpenSSH uses key or agent authentication by default. Setting `password_env` selects password authentication and requires a populated named variable.

### 13.1 Inventory validation

Validate the inventory:

```bash
tools/deployment/deploy_hosts.py plan
```

`plan` checks for unique host IDs, one host per role, distinct destination entries, and set access variables.

`check-access` verifies one connection per host. `shell --role <role>` resolves the role's host from the inventory.

### 13.2 Known-hosts handling

`NARWHAL_SSH_KNOWN_HOSTS` selects the SSH known-hosts file. Discovery uses `config/ssh.known_hosts` when the variable is unset.

Discovery uses OpenSSH `accept-new`:

- the first connection records the host key
- OpenSSH rejects a changed key
- existing verified stores keep prior entries

Deployment commands use strict host-key checking against this file.

The workstation needs OpenSSH. Password authentication also needs `sshpass`, which receives the secret through a private file descriptor.

For key or agent authentication, put address, username, identity, and optional `Port` or `ProxyJump` configuration in the workstation's private SSH config. Point `ssh_env` at that alias.

To replace the key of a changed server:

1. Verify the replacement through the supplied private access source.
2. Update the checkout-local known-hosts file.

Rely on SSH host keys to establish server identity; `hostname` output is only a label.

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

A source or configuration mismatch stops the helper at that host and leaves remote content untouched.

Private local logs under the run's `logs/` directory retain executed scripts, exit status, and host output.

A role shell opened with `--run` enters the deployed checkout, loads the matching role environment, and activates its virtual environment.
