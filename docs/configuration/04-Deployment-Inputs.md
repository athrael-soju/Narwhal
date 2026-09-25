# Deployment inputs and SSH trust

## 12. Deployment inputs and generated artifacts

Deployment discovery derives the host inventory, SSH trust store, fleet, launch records, and source index from the supplied `.env` and remote inspection.

[Launch policy](../deploy/01-Discover.md#confirm-launch-policy) defines defaults and environment overrides.

Keep these generated private inputs together for one inspected fleet:

```text
config/hosts.local.json
config/ssh.known_hosts
config/engine-launch.local.json
config/engine-launch.sources.json
config/fleet.json
config/deployment.env
```

Discovery stores per-engine checkpoint file hashes plus a matching tree digest under:

```text
runs/discovery/<run>/
```

The same directory retains observations and command logs.

Load `.env` and `config/deployment.env` before reusing saved discovery inputs.

Engine inspection in [Deploy](../deploy/03-Validate-Engines.md#inspect-every-engine-host) checks current host, image, and model configuration before launch.

Prepared workstation files live in the Git-ignored `runs/deployment-env/` directory.

Remote role environments and the effective fleet live in the Git-ignored `runs/deployment/` directory.

### 12.1 Source revision and deployment bundle

`NARWHAL_DEPLOYMENT_REVISION` is the full commit SHA from the management checkout.

Prepare deployment inputs with:

```bash
python3 tools/deployment/deploy_hosts.py prepare --out <directory>
```

`prepare` packages the selected revision as `source.bundle`, then verifies it by cloning the bundle into a fresh local checkout.

`install` copies that bundle to each selected host. Each remote checkout clones from it and verifies the checkout revision against the role file before installation.

Store `source.bundle` beside the generated role environments under ignored `runs/deployment-env/`.

`tools/deployment/prepare_host_env.py`:

- selects exported fields
- preserves shell literals
- writes role files with mode 0600

[Host installation](../deploy/02-Install.md) defines the full preparation, installation, and role-shell sequence.

### 12.2 Generated host-side files

| Remote file                                   | Contents                                                                                                                                                            | Source                                                                                                                                                                                                   |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `runs/deployment/.env.router`                 | Revision, `NARWHAL_FLEET=runs/deployment/fleet.json`, referenced engine/attestation URLs, configured engine credential, optional router and observability settings. | `NARWHAL_DEPLOYMENT_REVISION`, variables referenced by fleet endpoint fields and `engine.engine_api_key_env`, `NARWHAL_ROUTER_URL`, `NARWHAL_GRAFANA_BIND_ADDRESS`, `NARWHAL_PROMETHEUS_LISTEN_ADDRESS`. |
| `runs/deployment/.env.engine-<n>`             | Revision, launch and artifact fields, selected node URLs, fabric peer addresses, configured engine credential.                                                      | Shared engine fields, optional `NARWHAL_NODE_<n>_<field>` overrides, derived service URLs/fabric addresses, configured engine credential.                                                                |
| `config/engine-launch.engine-<n>.json`        | GPU allocation, TP size, device mappings, resolved UCX selection, generated launch arguments.                                                                       | The engine role in workstation `NARWHAL_LAUNCH_CONFIG`.                                                                                                                                                  |
| `runs/deployment-tools/launch_engine.py`      | Standalone launcher snapshot. Path and SHA-256 are recorded in the engine role environment.                                                                         | Management-checkout `tools/deployment/launch_engine.py`.                                                                                                                                                 |
| `runs/deployment-tools/fabric_budget.py`      | Standalone fabric calculator snapshot. Path and SHA-256 are recorded in `.env.engine-<n>`.                                                                          | Management-checkout `tools/deployment/fabric_budget.py`.                                                                                                                                                 |
| `runs/deployment-tools/cache_capture_hook.py` | Serving cache-capture snapshot. Path and SHA-256 are recorded in `.env.engine-<n>`.                                                                                 | Management-checkout `tools/deployment/cache_capture_hook.py`.                                                                                                                                            |
| `runs/deployment/fleet.json`                  | Effective generated fleet copied before deployment edits.                                                                                                           | Workstation file selected by `NARWHAL_FLEET`.                                                                                                                                                            |

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

The exporter also sets:

```text
NARWHAL_ENGINE_LAUNCH_CONFIG=config/engine-launch.engine-<n>.json
```

Per-node overrides insert `NODE_<n>_` after `NARWHAL_`.

For example, `NARWHAL_NODE_2_ENGINE_PORT` becomes `NARWHAL_ENGINE_PORT` inside `.env.engine-2`.

An empty override for a required field is an error against that field.

Fleet endpoint references and engine API-key references select variables by name.

The explicit role field set supplies export values. Variable names containing `SSH` and access variables referenced by the host inventory trigger a role-export error.

Management destinations, passwords, SSH identities, and host keys remain in the workstation's private access files.

### 12.3 Profiling limits

Deployment preparation writes `profiling-limits.json` beside the router's effective fleet.

Each engine limit comes from the generated launch record's `--max-num-seqs`.

Run profiling with the generated limits to bind decode cohorts to those launch settings:

```bash
narwhal-profile --limits runs/deployment/profiling-limits.json
```

### 12.4 Role shells

Load a generated role environment with:

```bash
deploy_hosts.py shell --run <directory> --role <role>
```

Shell tracing is disabled while the role file is loaded.

A host serving both router and engine roles keeps both files in one checkout. Each role shell loads only its own environment.

---

## 13. Host inventory and SSH trust

`NARWHAL_HOSTS` selects the physical-host inventory. Its default is:

```text
config/hosts.local.json
```

Discovery groups roles with identical `NARWHAL_NODE_<n>_SSH` or `NARWHAL_ROUTER_SSH` destinations into a single physical-host record.

Each `hosts` entry contains:

- unique `id`
- `ssh_env`, naming the environment variable holding the management destination
- optional `password_env`
- assigned `roles`

The `router` role appears once. Engine roles are named `engine-<n>` and map to the numbered deployment variables.

Colocated roles belong to one host entry.

The [example inventory](https://github.com/athrael-soju/Narwhal/blob/main/config/hosts.example.json) assigns `router` and `engine-1` to one machine and `engine-2` to another.

Keep destination values and credentials in workstation `.env`; the inventory stores variable names only.

SSH destinations may be aliases or `user@management-host`.

OpenSSH uses key or agent authentication by default. Setting `password_env` selects password authentication and requires a populated named variable.

All roles on one physical host share that host's access record.

### 13.1 Inventory validation

Run the inventory validation command:

```bash
tools/deployment/deploy_hosts.py plan
```

It checks:

- unique host IDs
- unique role ownership
- required access variables
- distinct destination entries

Put every role on a physical machine in one inventory entry. Discovery groups matching SSH destinations under one host ID. `check-access` verifies one connection to that host, and `shell --role <role>` resolves it from the inventory.

### 13.2 Known-hosts handling

A fresh management checkout starts from `.env`; discovery writes generated private files with mode 0600.

`NARWHAL_SSH_KNOWN_HOSTS` selects:

```text
config/ssh.known_hosts
```

Discovery uses OpenSSH `accept-new`:

- first connection records the host key
- a changed key is rejected
- existing verified stores retain prior entries

Deployment commands then use strict host-key checking against this file.

For password authentication, the secret reaches `sshpass` through a private file descriptor. The workstation therefore needs:

- OpenSSH
- `sshpass` when password authentication is used

For key or agent authentication, put address, username, identity, and optional `Port` or `ProxyJump` configuration in the workstation's private SSH config, then point `ssh_env` at that alias.

Before replacing a key for a changed server, verify the replacement through the supplied private access source, then update the checkout-local known-hosts file.

Treat remote `hostname` output as an observed label. SSH host keys establish server identity.

### 13.3 Prepared and remote runs

`prepare --out <directory>` writes:

- one verified source bundle
- role files grouped by host ID
- a private manifest containing revision, host assignments, destination fingerprints, and file hashes

Use a new output directory for each deployment.

`install --run <directory>` checks the manifest against current host mappings and local files, then transfers and installs once per selected host.

`--role engine-1` selects the host owning `engine-1`, including colocated roles.

`--host <id>` selects one physical host directly.

Repeating an installation verifies existing content and reuses it if the completion marker matches the approved revision.

Each remote run lives under:

```text
~/Narwhal-deploy/<id>/
```

The directory contains:

- source bundle
- role files
- `checkout/`
- installation lock
- completion marker

A source or configuration mismatch stops that host before the deployment helper advances to the next one. Existing remote content is preserved.

Private local logs under the run's `logs/` directory retain executed scripts, exit status, and host output.

A role shell opened with `--run` enters the deployed checkout, loads the matching role environment, and activates its virtual environment.
