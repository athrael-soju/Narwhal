---
description: The deployment environment, generated artifacts, host inventory and SSH trust for a Narwhal fleet.
---

# Deployment inputs and SSH trust

## 12. Deployment inputs and generated artifacts

The workstation `.env` holds the deployment revision, engine image, model and run paths, service ports, SSH destinations, and [launch-policy overrides](../deploy/01-Discover.md#confirm-the-launch-policy).

Discovery writes one private set of files per fleet at mode 0600:

| File                         | Selected by               | Default path                                           |
| ---------------------------- | ------------------------- | ------------------------------------------------------ |
| Host inventory               | `NARWHAL_HOSTS`           | `config/hosts.local.json`                              |
| SSH known-hosts store        | `NARWHAL_SSH_KNOWN_HOSTS` | `config/ssh.known_hosts`                               |
| Launch records               | `NARWHAL_LAUNCH_CONFIG`   | `config/engine-launch.local.json`                      |
| Launch-record sources        |                           | `engine-launch.sources.json` beside the launch records |
| Fleet                        | `NARWHAL_FLEET`           | `config/fleet.json`                                    |
| Derived deployment variables |                           | `deployment.env` beside the fleet                      |

| Directory                    | Host            | Contents                                                                                  |
| ---------------------------- | --------------- | ----------------------------------------------------------------------------------------- |
| `runs/discovery/<run>/`      | Workstation     | Per-engine checkpoint file hashes, a matching tree digest, observations, and command logs |
| `runs/deployment-env/<run>/` | Workstation     | Prepared source bundle, role files by host ID, manifest, and command logs                 |
| `runs/deployment/`           | Remote checkout | Role environments, the effective fleet, and `profiling-limits.json`                       |

Load `.env` and `config/deployment.env` before reusing saved discovery inputs.

### 12.1 Source revision and deployment bundle

`NARWHAL_DEPLOYMENT_REVISION` is the full commit SHA from the management checkout.

Prepare deployment inputs for [host installation](../deploy/02-Install.md):

```bash
python3 tools/deployment/deploy_hosts.py prepare --out <directory>
```

| Command                                | Output                                                                                                |
| -------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| `deploy_hosts.py prepare`              | `<directory>/source.bundle` with the selected revision, and role files under `<directory>/<host-id>/` |
| `tools/deployment/prepare_host_env.py` | Role files at mode 0600, with shell literals copied through unchanged                                 |
| `deploy_hosts.py install`              | The bundle and a checkout cloned from it on each selected host                                        |

### 12.2 Generated host-side files

| Remote file                                   | Contents                                                                                                                                                                       | Source                                                                                                                                                                                                   |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `runs/deployment/.env.router`                 | Revision, `NARWHAL_FLEET=runs/deployment/fleet.json`, referenced engine and attestation URLs, configured engine credential, and optional router and monitoring stack settings. | `NARWHAL_DEPLOYMENT_REVISION`, variables referenced by fleet endpoint fields and `engine.engine_api_key_env`, `NARWHAL_ROUTER_URL`, `NARWHAL_GRAFANA_BIND_ADDRESS`, `NARWHAL_PROMETHEUS_LISTEN_ADDRESS`. |
| `runs/deployment/.env.engine-<n>`             | Revision, launch and artifact fields, selected node URLs, fabric peer addresses, and configured engine credential.                                                             | Shared engine fields, optional `NARWHAL_NODE_<n>_<field>` overrides, derived service URLs and fabric addresses, and configured engine credential.                                                        |
| `config/engine-launch.engine-<n>.json`        | GPU allocation, TP size, device mappings, resolved UCX selection, and generated launch arguments.                                                                              | The engine role in workstation `NARWHAL_LAUNCH_CONFIG`.                                                                                                                                                  |
| `runs/deployment-tools/launch_engine.py`      | Standalone launcher snapshot, with its path and SHA-256 in `.env.engine-<n>`.                                                                                                  | Management-checkout `tools/deployment/launch_engine.py`.                                                                                                                                                 |
| `runs/deployment-tools/fabric_budget.py`      | Standalone fabric calculator snapshot, with its path and SHA-256 in `.env.engine-<n>`.                                                                                         | Management-checkout `tools/deployment/fabric_budget.py`.                                                                                                                                                 |
| `runs/deployment-tools/cache_capture_hook.py` | Serving cache-capture snapshot, with its path and SHA-256 in `.env.engine-<n>`.                                                                                                | Management-checkout `tools/deployment/cache_capture_hook.py`.                                                                                                                                            |
| `runs/deployment/fleet.json`                  | Effective generated fleet copied before deployment edits.                                                                                                                      | Workstation file selected by `NARWHAL_FLEET`.                                                                                                                                                            |
| `runs/deployment/profiling-limits.json`       | One `--max-num-seqs` per engine ID, on the router host.                                                                                                                        | Each engine's generated launch record.                                                                                                                                                                   |

Engine export requires:

```text
NARWHAL_DEPLOYMENT_REVISION
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

With `engine.engine_api_key_env` set, engine export requires the variable it names.

Each `.env.engine-<n>` sets `NARWHAL_ENGINE_LAUNCH_CONFIG=config/engine-launch.engine-<n>.json`.

Per-node overrides insert `NODE_<n>_` after `NARWHAL_`:

| Shared variable       | Override for `.env.engine-2` |
| --------------------- | ---------------------------- |
| `NARWHAL_ENGINE_PORT` | `NARWHAL_NODE_2_ENGINE_PORT` |

Export fails when:

- a required value is unset or empty
- a per-node override is set to an empty value
- `NARWHAL_DEPLOYMENT_REVISION` fails to match a full 40- or 64-hex commit SHA
- a variable name contains `SSH`
- a variable name matches an access variable from the host inventory

### 12.3 Profiling limits

Profile with the generated limits from the router shell:

```bash
.venv/bin/narwhal-profile --fleet runs/deployment/fleet.json --limits runs/deployment/profiling-limits.json
```

### 12.4 Role shells

Load a generated role environment:

```bash
python3 tools/deployment/deploy_hosts.py shell --run <directory> --role <role>
```

A host with both roles holds both role files in one checkout.

---

## 13. Host inventory and SSH trust

`NARWHAL_HOSTS` selects the physical-host inventory (default `config/hosts.local.json`).

Roles with identical `NARWHAL_NODE_<n>_SSH` or `NARWHAL_ROUTER_SSH` destinations share one physical-host record.

Each `hosts` entry contains:

| Field          | Value                                                                                          |
| -------------- | ---------------------------------------------------------------------------------------------- |
| `id`           | Unique lowercase host ID                                                                       |
| `ssh_env`      | Environment variable that holds the management destination, an alias or `user@management-host` |
| `password_env` | Optional environment variable that holds the SSH password                                      |
| `roles`        | Assigned roles                                                                                 |

Role names:

- `router`, assigned once
- `engine-<n>`, where `<n>` matches the numbered deployment variables

The [example inventory](https://github.com/athrael-soju/Narwhal/blob/main/config/hosts.example.json) assigns `router` and `engine-1` to one machine and `engine-2` to another.

| Value                              | Location           |
| ---------------------------------- | ------------------ |
| Destination values and credentials | Workstation `.env` |
| Variable names                     | Inventory          |

| `password_env` | Authentication                                            |
| -------------- | --------------------------------------------------------- |
| Omitted        | OpenSSH key or agent authentication                       |
| Set            | Password authentication from the populated named variable |

### 13.1 Inventory validation

Validate the inventory:

```bash
python3 tools/deployment/deploy_hosts.py plan
```

| Command               | Action                                                                                                                                           |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| `plan`                | Checks for unique host IDs, one host per role, a `router` role, at least one engine role, distinct destination entries, and set access variables |
| `check-access`        | Verifies one connection per host                                                                                                                 |
| `shell --role <role>` | Resolves the role's host from the inventory                                                                                                      |

### 13.2 Known-hosts handling

`NARWHAL_SSH_KNOWN_HOSTS` selects the SSH known-hosts file.

| Command             | `NARWHAL_SSH_KNOWN_HOSTS`                     | Host-key checking                          |
| ------------------- | --------------------------------------------- | ------------------------------------------ |
| Discovery           | Optional, default `config/ssh.known_hosts`    | OpenSSH `accept-new`                       |
| Deployment commands | Required, loaded from `config/deployment.env` | Strict host-key checking against this file |

Under `accept-new`:

- the first connection records the host key
- OpenSSH rejects a changed key
- existing verified stores keep prior entries

The workstation needs:

- OpenSSH
- `sshpass`, for password authentication

For key or agent authentication:

1. Put the address, username, identity, and optional `Port` or `ProxyJump` configuration under an alias in the workstation's private SSH config.
2. Point `ssh_env` at that alias.

To replace the key of a changed server:

1. Verify the replacement through the private access source.
2. Update the checkout-local known-hosts file.

### 13.3 Prepared and remote runs

`prepare --out <directory>` writes:

- one verified source bundle
- role files grouped by host ID
- a private manifest containing revision, host assignments, destination fingerprints, remote run directory, and file hashes

`--out` takes a new directory for each deployment.

`install --run <directory>` installs the prepared run once on each selected host.

| `install` option | Selected hosts                                      |
| ---------------- | --------------------------------------------------- |
| `--role <role>`  | The host that runs `<role>` and its colocated roles |
| `--host <id>`    | One physical host by inventory ID                   |
| Neither          | Every inventory host                                |

Each remote run at `~/Narwhal-deploy/<run-id>/` holds:

- source bundle
- role files
- `checkout/`
- installation lock
- `installed` completion marker

`<run-id>` is a random 32-hex ID that `prepare` records in the manifest.

Re-running `install` on a host:

| Existing remote content                                 | Result                     |
| ------------------------------------------------------- | -------------------------- |
| File with the manifest SHA-256                          | Reused                     |
| `checkout/` at the approved revision                    | Reused                     |
| `installed` marker at the approved revision             | Virtual environment reused |
| Changed file, or checkout or marker at another revision | Install stops at that host |

The run's private `logs/` directory holds one log per host with executed scripts, exit status, and host output.

A role shell opened with `--run` has:

- the deployed checkout as its working directory
- the matching role environment loaded
- the checkout's virtual environment active
