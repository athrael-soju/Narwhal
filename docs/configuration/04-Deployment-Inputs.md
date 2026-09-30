# Deployment inputs and SSH trust

## 12. Deployment inputs and generated artifacts

Deployment discovery reads the supplied `.env`, inspects the remote hosts, and generates the files listed here. Launch defaults and environment overrides are defined in the [launch policy](../deploy/01-Discover.md#confirm-launch-policy).

Use the generated files as a set from the same discovery run:

```text
config/hosts.local.json
config/ssh.known_hosts
config/engine-launch.local.json
config/engine-launch.sources.json
config/fleet.json
config/deployment.env
```

Each discovery run is recorded under `runs/discovery/<run>/`. The record holds per-engine checkpoint file hashes with a matching tree digest, the observations, and the command logs.

Load both `.env` and `config/deployment.env` before reusing saved discovery inputs. Before launch, [engine inspection](../deploy/03-Validate-Engines.md#inspect-every-engine-host) rechecks the current host, image, and model configuration.

Prepared workstation files go in `runs/deployment-env/`. Remote role environments and the effective fleet go in `runs/deployment/`. Git ignores both directories.

### 12.1 Source revision and deployment bundle

`NARWHAL_DEPLOYMENT_REVISION` is the full commit SHA of the management checkout. Prepare deployment inputs with:

```bash
python3 tools/deployment/deploy_hosts.py prepare --out <directory>
```

`prepare` packages the selected revision as `source.bundle` and verifies it by cloning the bundle into a fresh local checkout. The bundle lands in the output directory next to the generated role environments, so put that directory under `runs/deployment-env/`. Later, `install` copies it to each selected host, where the remote checkout clones from it and confirms that its revision matches the prepared revision before installing anything.

The role files come from `tools/deployment/prepare_host_env.py`, which selects the exported fields, leaves shell literals intact, and writes each file with mode 0600. See [Host installation](../deploy/02-Install.md) for the procedure.

### 12.2 Generated host-side files

| Remote file                                   | Contents                                                                                                                                                                        | Source                                                                                                                                                                                                   |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `runs/deployment/.env.router`                 | Revision, `NARWHAL_FLEET=runs/deployment/fleet.json`, referenced engine and attestation URLs, the configured engine credential, and optional router and observability settings. | `NARWHAL_DEPLOYMENT_REVISION`, variables referenced by fleet endpoint fields and `engine.engine_api_key_env`, `NARWHAL_ROUTER_URL`, `NARWHAL_GRAFANA_BIND_ADDRESS`, `NARWHAL_PROMETHEUS_LISTEN_ADDRESS`. |
| `runs/deployment/.env.engine-<n>`             | Revision, launch and artifact fields, selected node URLs, fabric peer addresses, and the configured engine credential.                                                          | Shared engine fields, optional `NARWHAL_NODE_<n>_<field>` overrides, derived service URLs and fabric addresses, the configured engine credential.                                                        |
| `config/engine-launch.engine-<n>.json`        | GPU allocation, TP size, device mappings, resolved UCX selection, and generated launch arguments.                                                                               | The engine role in the workstation's `NARWHAL_LAUNCH_CONFIG`.                                                                                                                                            |
| `runs/deployment-tools/launch_engine.py`      | Standalone launcher snapshot. Its path and SHA-256 are recorded in the engine role environment.                                                                                 | `tools/deployment/launch_engine.py` in the management checkout.                                                                                                                                          |
| `runs/deployment-tools/fabric_budget.py`      | Standalone fabric calculator snapshot. Its path and SHA-256 are recorded in `.env.engine-<n>`.                                                                                  | `tools/deployment/fabric_budget.py` in the management checkout.                                                                                                                                          |
| `runs/deployment-tools/cache_capture_hook.py` | Serving cache-capture snapshot. Its path and SHA-256 are recorded in `.env.engine-<n>`.                                                                                         | `tools/deployment/cache_capture_hook.py` in the management checkout.                                                                                                                                     |
| `runs/deployment/fleet.json`                  | The effective generated fleet, copied before any deployment edits.                                                                                                              | The workstation file selected by `NARWHAL_FLEET`.                                                                                                                                                        |

The workstation environment must define these variables to generate `.env.engine-<n>`:

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

The exporter sets `NARWHAL_ENGINE_LAUNCH_CONFIG=config/engine-launch.engine-<n>.json` automatically.

To override a value for a single node, insert `NODE_<n>_` after `NARWHAL_`. For example, `NARWHAL_NODE_2_ENGINE_PORT` becomes `NARWHAL_ENGINE_PORT` inside `.env.engine-2`.

Export values come from an explicit set of role fields, plus any variables that fleet endpoint references and the engine API-key reference pick out by name. The export fails if a variable whose name contains `SSH`, or an access variable referenced by the host inventory, would end up in a role file. Management destinations, passwords, SSH identities, and host keys stay in the workstation's private access files.

### 12.3 Profiling limits

Preparation writes `profiling-limits.json` next to the router's effective fleet. Each engine's limit is the `--max-num-seqs` value from its generated launch record. Profile with these limits so that decode cohorts match the launch `--max-num-seqs`:

```bash
narwhal-profile --limits runs/deployment/profiling-limits.json
```

### 12.4 Role shells

Open a shell for a generated role environment with:

```bash
python3 tools/deployment/deploy_hosts.py shell --run <directory> --role <role>
```

Shell tracing is disabled while the role file loads.

When one host runs both the router and an engine, both role files live in the same checkout. Each role shell loads only its own.

---

## 13. Host inventory and SSH trust

`NARWHAL_HOSTS` points at the physical-host inventory, which defaults to:

```text
config/hosts.local.json
```

Keep every role on a physical machine in one inventory entry, so that `check-access` verifies a single connection to the host and `shell --role <role>` can resolve the host. Discovery does this by merging roles whose `NARWHAL_NODE_<n>_SSH` or `NARWHAL_ROUTER_SSH` destinations are identical into one physical-host record. Colocated roles share that host's access record. Each entry in `hosts` has a unique `id`, an `ssh_env` naming the environment variable that holds the management destination, an optional `password_env`, and the list of `roles` assigned to it. The `router` role appears exactly once, and engine roles are named `engine-<n>` to match the numbered deployment variables. The [example inventory](https://github.com/athrael-soju/Narwhal/blob/main/config/hosts.example.json) puts `router` and `engine-1` on one machine and `engine-2` on another.

The inventory stores only variable names. Destination values and credentials stay in the workstation `.env`. A destination can be an SSH alias or `user@management-host`. OpenSSH uses key or agent authentication unless `password_env` is set, in which case it uses password authentication and the named variable must be populated.

### 13.1 Inventory validation

Validate the inventory with:

```bash
python3 tools/deployment/deploy_hosts.py plan
```

The command checks that host IDs are unique, that each role has exactly one owner, that the required access variables are set, and that destination entries are distinct.

### 13.2 Known-hosts handling

A fresh management checkout starts from `.env`, and discovery writes the private files it generates with mode 0600. `NARWHAL_SSH_KNOWN_HOSTS` points at the SSH trust store, `config/ssh.known_hosts`.

Discovery connects with OpenSSH's `accept-new` policy:

- The first connection to a host records its key.
- A changed key is rejected.
- Entries already in a verified store are kept.

Every deployment command after that checks host keys strictly against this file. If a server's key changes for a legitimate reason, confirm the new key through your private access source before updating the checkout-local known-hosts file. Output from `hostname` on a remote machine is only a label; the SSH host key is what identifies the server.

With password authentication, the password is handed to `sshpass` through a private file descriptor, so the workstation needs `sshpass` as well as OpenSSH. For key or agent authentication, put the address, username, identity, and any `Port` or `ProxyJump` settings in the workstation's private SSH config and set the variable that `ssh_env` names to that alias.

### 13.3 Prepared and remote runs

`prepare --out <directory>` writes one verified source bundle, the role files grouped by host ID, and a private manifest recording the revision, host assignments, destination fingerprints, and file hashes. Use a new output directory for every deployment.

`install --run <directory>` checks the manifest against the current host mappings and local files, then transfers and installs once per selected host. Select hosts with:

- `--role engine-1`: the host that owns `engine-1`, along with any roles colocated there.
- `--host <id>`: a physical host directly.

Running `install` again verifies what is already on the host and reuses it if the completion marker matches the prepared revision.

Each remote run lives in `~/Narwhal-deploy/<run-id>/`, where `<run-id>` is a random ID that `prepare` records in the manifest. The directory holds the source bundle, the role files, `checkout/`, an installation lock, and the completion marker. If a host's source or configuration does not match, installation stops at that host, does not continue to the remaining hosts, and leaves existing remote content unchanged. The run's local `logs/` directory on the workstation holds private logs of the executed scripts, their exit status, and host output.

A role shell opened with `--run` changes into the deployed checkout, loads the matching role environment, and activates its virtual environment.
