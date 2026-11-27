---
description: Subcommands, options, host-key checks, time limits and exit codes of narwhal deploy on SSH hosts.
---

# `narwhal deploy`

`narwhal deploy` runs the workstation side of [deploying a fleet](../Deploy.md) to SSH hosts: discovery, the access check, packaging, installation and the cache-equivalence groups. It also opens role shells and port tunnels. Run it with `.env` loaded, from the directory that holds `config/`:

```bash
narwhal deploy discover --out runs/discovery/first-deploy
. config/deployment.env
narwhal deploy access
narwhal deploy package --out runs/deployment-env/first-deploy
narwhal deploy install --run runs/deployment-env/first-deploy
narwhal deploy shell --run runs/deployment-env/first-deploy --role router
narwhal deploy tunnel --forward 18000:8000
```

| Subcommand | Operation | Procedure |
| --- | --- | --- |
| `discover` | Logs in to every host that the `NARWHAL_NODE_<n>_SSH` and `NARWHAL_ROUTER_SSH` destinations name, inspects the GPUs, the engine image and the checkpoint on each engine host, checks each checkpoint against `NARWHAL_MODEL_TREE_SHA256`, and writes the host inventory, launch records, fleet and `deployment.env`. | [Running discovery and access checks](../deploy/01-Discover.md#running-discovery-and-access-checks) |
| `access` | Checks the inventory, then logs in once to each host and records its `hostname` output. Prints each host ID with its roles. | [Running discovery and access checks](../deploy/01-Discover.md#running-discovery-and-access-checks) |
| `package` | Checks the release wheel that `NARWHAL_WHEEL` names and the dependency lock that `NARWHAL_REQUIREMENTS` names against their SHA-256 values, then writes them, the helper snapshots from it and each host's role files into a new directory with a private manifest. | [Building the deployment package](../deploy/02-Install.md#building-the-deployment-package) |
| `install` | Copies a package to each selected host, checking the SHA-256 of every file, and installs the locked dependencies and the wheel into a virtual environment in the host's deployment directory. | [Installing engine 1 and the remaining hosts](../deploy/02-Install.md#installing-engine-1-and-the-remaining-hosts) |
| `cache-groups` | Groups the engine roles of the launch records that `NARWHAL_LAUNCH_CONFIG` names, else `config/engine-launch.local.json`, by their discovered image and model configuration, their allocation, runtime and transport, and each engine's captured cache layout or measured KV handoff, and writes `cache-groups.json`. Prints each group's ID, representative and roles. | [Deriving cache-equivalence groups](../deploy/03-Validate-Engines.md#deriving-cache-equivalence-groups) |
| `shell` | Opens an interactive login shell on a role's host. With `--run`, the shell starts in the deployment directory with the role environment loaded and `.venv` active. | [Opening installed role shells](../deploy/02-Install.md#opening-installed-role-shells) |
| `tunnel` | Forwards workstation loopback ports to a role's host until you press Ctrl+C. | [Tunnelling router, Prometheus, and Grafana to the workstation](../deploy/07-Serve-and-Measure.md#tunnelling-router-prometheus-and-grafana-to-the-workstation) |

`access`, `package`, `install`, `shell` and `tunnel` read the inventory that discovery writes. [Host inventory and SSH trust](../configuration/04-Deployment-Inputs.md#host-inventory-and-ssh-trust) describes it.

## Host keys and time limits

Every connection checks the host's key against `NARWHAL_SSH_KNOWN_HOSTS` alone, with OpenSSH `StrictHostKeyChecking=yes`, before it authenticates, whatever your SSH config sets. `narwhal deploy` never adds or replaces a key in that file. A host whose key is missing from the file or differs from it fails with `<host-id>: blocked at <operation>: the host key differs from NARWHAL_SSH_KNOWN_HOSTS or is missing from it`. [Known-hosts handling](../configuration/04-Deployment-Inputs.md#known-hosts-handling) lists the options that override your SSH config, and [Jump host](../configuration/04-Deployment-Inputs.md#jump-host) how to reach the hosts through one.

Key authentication runs with OpenSSH `BatchMode=yes`, so a key that needs a passphrase fails instead of prompting. Add it to your SSH agent first. `shell` can prompt. Password authentication passes the password to OpenSSH through a private askpass helper and a FIFO, never a file. Every connection needs OpenSSH 8.5 or later.

Each remote command runs under the time limit of its stage:

| Stage | Remote commands | Limit |
| --- | --- | --- |
| `access` | The login check of `discover` and `access` | 60 s |
| `discover` | GPU and image inspection, checkpoint hashing | 3,600 s |
| `install` | File checks, file transfers and the wheel installation | 1,800 s |

`NARWHAL_STAGE_<STAGE>_TIMEOUT_SECONDS` sets the limit of one stage, and `NARWHAL_STAGE_TIMEOUT_SECONDS` sets the limit of every stage, including the [`narwhal dev` stages](../Dev-Runtime.md#stage-deadlines-and-recovery). For example, a checkpoint that takes longer than an hour to hash on an engine host needs:

```bash
NARWHAL_STAGE_DISCOVER_TIMEOUT_SECONDS=7200 narwhal deploy discover --out runs/discovery/first-deploy
```

The host runs each command under coreutils `timeout`, which sends SIGTERM to the command and every process it started at the limit, and SIGKILL 5 seconds later to a command that ignores SIGTERM. Both count as exceeding the limit. A host without `timeout` fails with `the host lacks the coreutils timeout command`. When the host has not answered 30 seconds after the limit, the workstation kills its `ssh`. The error names the host, the operation, the limit and the host's log, as in `node-2: hash checkpoint files exceeded the 3600s discover time limit; inspect runs/discovery/first-deploy/logs/node-2.log`. OpenSSH gives up on a connection after 30 seconds. `shell` and `tunnel` run until you leave them.

Each host's log holds the scripts that ran there, their exit status or time limit, and their output, at mode 0600. The log directory is `logs/` in the discovery output or the package directory, or `runs/access-<id>/` for `access`, `tunnel`, and `shell` without `--run`.

## Output and exit codes

In text mode, stdout carries progress lines, and a failure writes one stderr line, `narwhal: deploy <subcommand>: <cause>`.

| Outcome | Exit code |
| --- | :--: |
| Success | `0` |
| A remote command fails or exceeds its time limit, or another operating-system error | `1` |
| Invalid arguments, inventory, environment, package or discovery inputs, or an output that already exists | `2` |
| `tunnel` closed with Ctrl+C | `130` |

With `--format json`, `discover`, `access`, `package`, `install` and `cache-groups` print a [versioned command result](../Command-Results.md). A failed or timed-out remote command exits with code `4`. Its error carries the stage, and its `context` names the host, the operation and the log. `shell` and `tunnel` are interactive, and refuse `--format json` with exit code `2`.

## Options

`--format` applies to `discover`, `access`, `package`, `install` and `cache-groups`, and `--hosts` to every subcommand except `discover` and `cache-groups`. `--out` applies to `discover`, `package` and `cache-groups`, `--cache-layout` and `--transfer-record` to `cache-groups`, `--run` to `install` and `shell`, `--host` to `install`, `--role` to `install`, `shell` and `tunnel`, and `--forward` and `--remote-address` to `tunnel`.

| Option | Default | Description | Valid values |
| --- | --- | --- | --- |
| `--format` | `text` | Output format, either `text` or `json` for [versioned command results](../Command-Results.md). | |
| `--hosts` | `NARWHAL_HOSTS`, else `config/hosts.local.json` | Host inventory. | |
| `--out` | Required | For `discover` and `package`, a new directory for the discovery output or the package, created at mode 0700. For `cache-groups`, the directory for `cache-groups.json`, created at mode 0700 when absent. | For `discover` and `package`, a path that does not exist. For `cache-groups`, a directory without `cache-groups.json` |
| `--cache-layout` | None | `ROLE=PATH`: the `cache-layout.json` that `narwhal-engine capture-cache` wrote for the role. Repeatable, once for each engine that captures its cache layout. | An `engine-<n>` role of the launch records |
| `--transfer-record` | None | `ROLE=PATH`: the `transfer.json` of the role's measured KV handoffs. Repeatable, once for each engine whose handoffs are measured. | An `engine-<n>` role of the launch records |
| `--run` | Required for `install`, none for `shell` | Package directory that `package` wrote. | |
| `--host` | Every inventory host | Host to install, by inventory ID. Repeatable. Excludes `--role`. | An inventory host ID |
| `--role` | Every inventory host for `install`, required for `shell`, `router` for `tunnel` | For `install`, the role whose host to install, with its colocated roles. Repeatable. Excludes `--host`. For `shell` and `tunnel`, the role whose host to open. | `router` or `engine-<n>` from the inventory |
| `--forward` | Required | `LOCAL_PORT:REMOTE_PORT`. Forwards workstation `127.0.0.1:LOCAL_PORT` to `REMOTE_PORT` on `--remote-address` at the host. Repeatable. | Ports 1 to 65535, a distinct local port per forward |
| `--remote-address` | `127.0.0.1` | Address that the forwarded ports listen on at the host. | An IPv4 or IPv6 address |
