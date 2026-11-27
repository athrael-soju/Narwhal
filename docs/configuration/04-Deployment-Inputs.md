---
description: The deployment environment, trust inputs, generated artifacts, host inventory and SSH trust for a Narwhal fleet.
---

# Deployment inputs and SSH trust

## Deployment inputs and generated artifacts

The workstation `.env` holds the engine image, model and run paths, service ports, SSH destinations, the trust inputs below, and [launch-policy overrides](../deploy/01-Discover.md#confirming-the-launch-policy).

The trust inputs pin what the deployment accepts. The commands that read them refuse to run without them:

| Variable | Read by | Value |
| --- | --- | --- |
| `NARWHAL_SSH_KNOWN_HOSTS` | Every `narwhal deploy` subcommand that reaches a host | Known-hosts file with the verified key of every SSH destination, as in [Known-hosts handling](#known-hosts-handling) |
| `NARWHAL_MODEL_TREE_SHA256` | `narwhal deploy discover`, and `narwhal-engine prepare` through each engine's role environment | The checkpoint's `model_tree_sha256`, computed as in [Staging and identifying the checkpoint](../deploy/01-Discover.md#staging-and-identifying-the-checkpoint). `NARWHAL_NODE_<n>_MODEL_TREE_SHA256` sets it for one engine |
| `NARWHAL_WHEEL` | `narwhal deploy package` | Path of the release wheel, `narwhal_inference-<major>.<minor>.<patch>-py3-none-any.whl`, that every host installs |
| `NARWHAL_WHEEL_SHA256` | `narwhal deploy package`, then `narwhal deploy install` on each host | SHA-256 of that wheel, 64 lowercase hex digits |
| `NARWHAL_REQUIREMENTS` | `narwhal deploy package`, then `pip` on each host | Hash-checking lock of the wheel's dependencies, as in [Dependency lock](#dependency-lock) |
| `NARWHAL_REQUIREMENTS_SHA256` | `narwhal deploy package`, then `narwhal deploy install` on each host | SHA-256 of that lock |
| `NARWHAL_TOOLS_COMMIT` | `narwhal deploy package`, then the router host's [tools checkout](../deploy/02-Install.md#installing-the-repository-tools-on-the-router-host) | Full 40-hex commit of the wheel's release tag `v<version>`, from the private record |

`NARWHAL_SSH_JUMP` is optional. It names the one jump host, an alias or `user@host`, through which every connection reaches its destination, as in [Jump host](#jump-host).

Discovery writes one private set of files per fleet at mode 0600:

| File                         | Selected by             | Default path                                           |
| ---------------------------- | ----------------------- | ------------------------------------------------------ |
| Host inventory               | `NARWHAL_HOSTS`         | `config/hosts.local.json`                              |
| Launch records               | `NARWHAL_LAUNCH_CONFIG` | `config/engine-launch.local.json`                      |
| Launch-record sources        |                         | `engine-launch.sources.json` beside the launch records |
| Fleet                        | `NARWHAL_FLEET`         | `config/fleet.json`                                    |
| Derived deployment variables |                         | `deployment.env` beside the fleet                      |

| Directory                    | Host                 | Contents                                                                                     |
| ---------------------------- | -------------------- | -------------------------------------------------------------------------------------------- |
| `runs/discovery/<run>/`      | Workstation          | Per-engine checkpoint file hashes, the checked tree digest, observations, and command logs   |
| `runs/deployment-env/<run>/` | Workstation          | The packaged wheel, role files by host ID, manifest, and command logs                        |
| `~/Narwhal-deploy/<id>/`     | Each host            | The deployment directory, with `package/`, `.venv` and the role files at their working paths |
| `runs/deployment/`           | Deployment directory | Role environments, the effective fleet, and `profiling-limits.json`                          |

Load `.env` and `config/deployment.env` before reusing saved discovery inputs.

### Narwhal wheel and deployment package

Build the package for [host installation](../deploy/02-Install.md):

```bash
narwhal deploy package --out <directory>
```

| Command                  | Output                                                                                                         |
| ------------------------ | -------------------------------------------------------------------------------------------------------------- |
| `narwhal deploy package` | A copy of the wheel checked against `NARWHAL_WHEEL_SHA256`, and role files under `<directory>/<host-id>/`      |
| `narwhal deploy install` | The package, and a virtual environment with the wheel installed, in each selected host's deployment directory |

Each role environment sets `NARWHAL_DEPLOYMENT_REVISION` to `NARWHAL_WHEEL_SHA256`, and launch plans record it as `revision`. The router environment also carries `NARWHAL_TOOLS_COMMIT`.

### Dependency lock

Every host installs the same dependency files: `pip` runs in hash-checking mode with `--require-hashes --only-binary :all:`, from the lock that `NARWHAL_REQUIREMENTS` names, and installs the wheel itself with `--no-deps --no-index`. `pip check` then confirms that the lock satisfies every dependency of the wheel. The hosts still download the locked files from their Python package index.

Compile the lock from the wheel with `uv`, for every platform and Python version that the wheel supports, and record its SHA-256 with the wheel's:

```bash
echo "narwhal-inference @ file://$(realpath "$NARWHAL_WHEEL")" > requirements.in
uv pip compile requirements.in --universal --python-version 3.11 --generate-hashes \
  --no-emit-package narwhal-inference -o requirements.lock
sha256sum requirements.lock
```

Keep `requirements.lock` with the wheel, and set `NARWHAL_REQUIREMENTS` to its path. Compile it again only to take new dependency releases, and package a new run when you do.

`package` refuses a lock with a line that does not pin one exact version with its `--hash=sha256` values, a lock with pip options such as `--index-url`, and a lock that names `narwhal-inference`.

### Generated host-side files

Paths are relative to the deployment directory.

| Remote file                                   | Contents                                                                                                                                                                       | Source                                                                                                                                                                                                   |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `runs/deployment/.env.router`                 | Revision, `NARWHAL_FLEET=runs/deployment/fleet.json`, referenced engine and attestation URLs, configured engine credential, and optional router and monitoring stack settings. | `NARWHAL_WHEEL_SHA256`, variables referenced by fleet endpoint fields and `engine.engine_api_key_env`, `NARWHAL_ROUTER_URL`, `NARWHAL_TARGETS_URL`, `NARWHAL_GRAFANA_BIND_ADDRESS`, `NARWHAL_PROMETHEUS_LISTEN_ADDRESS`. |
| `runs/deployment/.env.engine-<n>`             | Revision, launch and artifact fields, helper snapshot paths and SHA-256 values, selected node URLs, fabric peer addresses, and configured engine credential.                  | `NARWHAL_WHEEL_SHA256`, shared engine fields, optional `NARWHAL_NODE_<n>_<field>` overrides, derived service URLs and fabric addresses, the packaged helper snapshots, and configured engine credential. |
| `config/engine-launch.engine-<n>.json`        | GPU allocation, TP size, device mappings, resolved UCX selection, and generated launch arguments.                                                                              | The engine role in workstation `NARWHAL_LAUNCH_CONFIG`.                                                                                                                                                  |
| `runs/deployment-tools/fabric_budget.py`      | Standalone fabric calculator snapshot, with its path and SHA-256 in `.env.engine-<n>`.                                                                                         | `narwhal/deployment/fabric_budget.py` in the wheel that `NARWHAL_WHEEL` names.                                                                                                                       |
| `runs/deployment-tools/cache_capture_hook.py` | Copy of the cache-capture hook for a vLLM or TensorRT-LLM fleet, with its path and SHA-256 in `.env.engine-<n>`.                                                               | `narwhal/backends/<backend>/cache_capture_hook.py` in the wheel that `NARWHAL_WHEEL` names, for `vllm` or `trtllm`.                                                                                    |
| `runs/deployment-tools/transfer_measure.py`   | Standalone KV handoff measurement tool for an SGLang fleet, with its path and SHA-256 in `.env.engine-<n>`.                                                                    | `narwhal/backends/sglang/transfer_measure.py` in the wheel that `NARWHAL_WHEEL` names.                                                                                                                |
| `runs/deployment/fleet.json`                  | Effective generated fleet copied before deployment edits.                                                                                                                      | Workstation file selected by `NARWHAL_FLEET`.                                                                                                                                                            |
| `runs/deployment/profiling-limits.json`       | Each engine's `runtime.max_sequences` by engine ID, or the backend default when unset (`256` on SGLang, `64` on TensorRT-LLM), on the router host.                                                               | Each engine's generated launch record.                                                                                                                                                                   |

Engine export requires:

```text
NARWHAL_ENGINE_IMAGE
NARWHAL_ENGINE_MODEL_NAME
NARWHAL_MODEL_DIR
NARWHAL_RUN_DIR
NARWHAL_MODEL_CONFIG_SHA256
NARWHAL_MODEL_TREE_SHA256
NARWHAL_FABRIC_INTERFACE
NARWHAL_ENGINE_PORT
NARWHAL_ATTEST_PORT
NARWHAL_UCX_TCP_PORT_RANGE
NARWHAL_KV_SIDE_CHANNEL_PORT
```

With `engine.engine_api_key_env` set, engine export requires the variable it names.

For a [GGUF model](../deploy/01-Discover.md#staging-a-gguf-model-on-vllm), engine export also carries `NARWHAL_MODEL_PATH` and `NARWHAL_MODEL_SHA256`, and fails when only one of them is set.

Each `.env.engine-<n>` sets `NARWHAL_ENGINE_LAUNCH_CONFIG=config/engine-launch.engine-<n>.json`.

Per-node overrides insert `NODE_<n>_` after `NARWHAL_`. For example, `NARWHAL_NODE_2_ENGINE_PORT` overrides `NARWHAL_ENGINE_PORT` in `.env.engine-2`.

Export fails when:

- a required value is unset or empty
- a per-node override is set to an empty value
- a variable name contains `SSH`
- a variable name matches an access variable from the host inventory

### Profiling limits

Profile with the generated limits from the router shell:

```bash
.venv/bin/narwhal-profile --fleet runs/deployment/fleet.json --limits runs/deployment/profiling-limits.json
```

### Role shells

Open a shell for a role on its host. With `--run`, the shell starts in the deployment directory with the role environment loaded and the deployment's virtual environment active, as [Opening installed role shells](../deploy/02-Install.md#opening-installed-role-shells) shows:

```bash
narwhal deploy shell --run <directory> --role <role>
```

A host with both roles holds both role files in one deployment directory.


## Host inventory and SSH trust

`narwhal deploy access`, `package`, `install`, `shell` and `tunnel` read the physical-host inventory from `--hosts PATH`, then from `NARWHAL_HOSTS`, then from `config/hosts.local.json`, as in `narwhal deploy access --hosts <path>`.

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

The workstation `.env` holds destination values and credentials, and the inventory holds the variable names.

With `password_env` omitted, SSH connections use OpenSSH key or agent authentication, which never prompts. With `password_env` set, they use password authentication from the populated named variable.

### Inventory validation

Every command that reads the inventory checks it first: unique host IDs, one host per role, a `router` role, at least one engine role, distinct destination entries, and set access variables. Check the inventory and every login:

```bash
narwhal deploy access
```

`access` prints each host ID with its roles once its login succeeds. `shell --role <role>` and `tunnel --role <role>` resolve the role's host from the inventory.

### Known-hosts handling

`NARWHAL_SSH_KNOWN_HOSTS` selects the SSH known-hosts file, such as `config/ssh.known_hosts`, which the publication check keeps out of the repository. Its path may not contain whitespace or `%`. Discovery and every later command require it. They check each host's key against this file alone, with OpenSSH `StrictHostKeyChecking=yes`, before they log in, and never write to it. A host whose key is missing from the file or differs from it stops the command at that host. The discovery manifest records the file's SHA-256.

Options on the command line take precedence over `~/.ssh/config` and `/etc/ssh/ssh_config`, so every connection also sets `KnownHostsCommand=none`, `VerifyHostKeyDNS=no`, `ControlMaster=no` and `ControlPath=none`, which stop it from consulting other keys or reusing a connection whose host key it did not check. It sets `ProxyJump=none` and `ProxyCommand=none`, so a jump host in your SSH config is ignored, and `PermitLocalCommand=no`, `RemoteCommand=none`, `ForwardAgent=no` and `ForwardX11=no`. It sets `SetEnv=NARWHAL_DEPLOY=1`, which displaces any `SetEnv` in your SSH config, because OpenSSH keeps the first value it reads. OpenSSH adds a config file's `SendEnv` to the command line's instead, so `ssh` runs with only `PATH`, `HOME`, `TERM`, `SSH_AUTH_SOCK` and `XDG_RUNTIME_DIR` from your environment, and `SendEnv` has nothing else to forward. `XDG_RUNTIME_DIR` stays so that an `IdentityAgent` written as `${XDG_RUNTIME_DIR}/…` still finds its socket. Your SSH config still supplies each alias's `HostName`, `User`, `Port` and `IdentityFile`.

Write the file before discovery, with one entry for each SSH destination. Take each key from a source you trust, such as the provider console, and compare fingerprints with `ssh-keygen -lf <file>`. For a destination alias, write the entry for its `HostName`, in `[host]:port` form when the port is not 22.

The workstation needs OpenSSH 8.5 or later. For password authentication, OpenSSH asks a private askpass helper, which reads the password from a FIFO that `narwhal deploy` holds open. The password stays in the pipe until OpenSSH reads it, and is never written to a file.

For key or agent authentication:

1. Put the address, username, identity, and optional `Port` configuration under an alias in the workstation's private SSH config.
2. Point `ssh_env` at that alias.

### Jump host

Set `NARWHAL_SSH_JUMP` to reach every host through one jump host. Each connection then runs `ssh -W` to the jump host with the same options, so the jump host's key must also be in `NARWHAL_SSH_KNOWN_HOSTS`. The jump connection authenticates with a key or the SSH agent, never a password.

To replace the key of a changed server:

1. Verify the replacement through the private access source.
2. Replace its entry in `NARWHAL_SSH_KNOWN_HOSTS`.

### Packages and deployment directories

`narwhal deploy package --out <directory>` writes the wheel, role files grouped by host ID and a private manifest, as [Building the deployment package](../deploy/02-Install.md#building-the-deployment-package) lists. Use a new directory for each deployment.

`narwhal deploy install --run <directory>` installs the package once on each selected host.

| `install` option | Selected hosts                                      |
| ---------------- | --------------------------------------------------- |
| `--role <role>`  | The host that runs `<role>` and its colocated roles |
| `--host <id>`    | One physical host by inventory ID                   |
| Default          | Every inventory host                                |

Each deployment directory at `~/Narwhal-deploy/<run-id>/` holds:

- `package/`, the transferred wheel and role files
- `.venv`, with the wheel installed
- the role files at their working paths under `runs/deployment/`, `runs/deployment-tools/` and `config/`
- the installation lock `.install-lock`, while `install` runs, with an `owner` file that names the installer's process, host and start time
- the `installed` completion marker, which holds the wheel's SHA-256

`<run-id>` is a random 32-hex ID that `package` records in the manifest.

A repeated `install` on a host reuses files that match their manifest SHA-256. With an `installed` marker for the packaged wheel, it reuses the virtual environment. A changed file, or a marker for another wheel, stops the install at that host.

The package's private `logs/` directory holds one log per host with executed scripts, exit status, and host output.
