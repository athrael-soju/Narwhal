---
description: Subcommands, options, host-key checks, time limits and exit codes of narwhal deploy on SSH hosts.
---

# `narwhal deploy`

`narwhal deploy` runs the workstation side of [deploying a fleet](../Deploy.md) to SSH hosts: discovery, the access check, packaging, installation, SGLang handoff measurement, the cache-equivalence groups and fabric qualification. It also opens role shells and port tunnels. Run it with `.env` loaded, from the directory that holds `config/`:

```bash
narwhal deploy discover --out runs/discovery/first-deploy
. config/deployment.env
narwhal deploy access
narwhal deploy package --out runs/deployment-env/first-deploy
narwhal deploy install --run runs/deployment-env/first-deploy
narwhal deploy transfer-measure --run runs/deployment-env/first-deploy --out runs/transfer-measure/first-deploy
narwhal deploy cache-groups --run runs/deployment-env/first-deploy --out runs/cache-groups/first-deploy \
  --cache-layout engine-1=runs/cache-layouts/engine-1.json --cache-layout engine-2=runs/cache-layouts/engine-2.json
narwhal deploy fabric --run runs/deployment-env/first-deploy \
  --groups runs/cache-groups/first-deploy/cache-groups.json --out runs/fabric/first-deploy
narwhal deploy shell --run runs/deployment-env/first-deploy --role router
narwhal deploy tunnel --forward 18000:8000
```

| Subcommand | Operation | Procedure |
| --- | --- | --- |
| `discover` | Logs in to every host that the `NARWHAL_NODE_<n>_SSH` and `NARWHAL_ROUTER_SSH` destinations name, inspects the GPUs, the engine image and the checkpoint on each engine host, checks each checkpoint against `NARWHAL_MODEL_TREE_SHA256`, and writes the host inventory, launch records, fleet and `deployment.env`. | [Running discovery and access checks](../deploy/01-Discover.md#running-discovery-and-access-checks) |
| `access` | Checks the inventory, then logs in once to each host and records its `hostname` output. Prints each host ID with its roles. | [Running discovery and access checks](../deploy/01-Discover.md#running-discovery-and-access-checks) |
| `package` | Checks the release wheel that `NARWHAL_WHEEL` names and the dependency lock that `NARWHAL_REQUIREMENTS` names against their SHA-256 values, then writes them, the helper snapshots from it and each host's role files into a new directory with a private manifest. | [Building the deployment package](../deploy/02-Install.md#building-the-deployment-package) |
| `install` | Copies a package to each selected host, checking the SHA-256 of every file, and installs the locked dependencies and the wheel into a virtual environment in the host's deployment directory. | [Installing engine 1 and the remaining hosts](../deploy/02-Install.md#installing-engine-1-and-the-remaining-hosts) |
| `transfer-measure` | Measures eight KV handoffs from every engine whose backend sizes its fabric budget from them, on SGLang, to the next engine role with the same launch inputs, and writes each engine's `transfer.json`. | [Measuring KV handoffs](../deploy/04-Qualify-Fabric.md#measuring-kv-handoffs) |
| `cache-groups` | Groups the engine roles of the launch records that `NARWHAL_LAUNCH_CONFIG` names, else `config/engine-launch.local.json`, by their discovered image and model configuration, their allocation, runtime and transport, and each engine's captured cache layout or measured KV handoff, and writes `cache-groups.json`. Prints each group's ID, representative and roles. | [Deriving cache-equivalence groups](../deploy/03-Validate-Engines.md#deriving-cache-equivalence-groups) |
| `fabric` | Sizes each cache group's KV budget, then checks the tools, routes, test port and RDMA GID entries of every directed edge between engine hosts and measures it with `iperf3` or `ib_write_bw`, or reuses a retained sample whose link record is unchanged, against the budget of every source role. | [Qualify the transfer fabric](../deploy/04-Qualify-Fabric.md) |
| `shell` | Opens an interactive login shell on a role's host. With `--run`, the shell starts in the deployment directory with the role environment loaded and `.venv` active. | [Opening installed role shells](../deploy/02-Install.md#opening-installed-role-shells) |
| `tunnel` | Forwards workstation loopback ports to a role's host until you press Ctrl+C. | [Tunnelling router, Prometheus, and Grafana to the workstation](../deploy/07-Serve-and-Measure.md#tunnelling-router-prometheus-and-grafana-to-the-workstation) |

`access`, `package`, `install`, `transfer-measure`, `fabric`, `shell` and `tunnel` read the inventory that discovery writes. [Host inventory and SSH trust](../configuration/04-Deployment-Inputs.md#host-inventory-and-ssh-trust) describes it.

## Host keys and time limits

Every connection checks the host's key against `NARWHAL_SSH_KNOWN_HOSTS` alone, with OpenSSH `StrictHostKeyChecking=yes`, before it authenticates, whatever your SSH config sets. `narwhal deploy` never adds or replaces a key in that file. A host whose key is missing from the file or differs from it fails with `<host-id>: blocked at <operation>: the host key differs from NARWHAL_SSH_KNOWN_HOSTS or is missing from it`. [Known-hosts handling](../configuration/04-Deployment-Inputs.md#known-hosts-handling) lists the options that override your SSH config, and [Jump host](../configuration/04-Deployment-Inputs.md#jump-host) how to reach the hosts through one.

Key authentication runs with OpenSSH `BatchMode=yes`, so a key that needs a passphrase fails instead of prompting. Add it to your SSH agent first. `shell` can prompt. Password authentication passes the password to OpenSSH through a private askpass helper and a FIFO, never a file. Every connection needs OpenSSH 8.5 or later.

Each remote command runs under the time limit of its stage:

| Stage | Remote commands | Limit |
| --- | --- | --- |
| `access` | The login check of `discover` and `access` | 60 s |
| `discover` | GPU and image inspection, checkpoint hashing | 3,600 s |
| `install` | File checks, file transfers and the wheel installation | 1,800 s |
| `transfer-measure` | Each engine's handoff measurement | 900 s |
| `fabric-budget` | Copying each representative's budget sources | 300 s |
| `fabric-edge` | Each tool, route, GID and port check, and each server start, client run and server stop | 300 s |

`NARWHAL_STAGE_<STAGE>_TIMEOUT_SECONDS` sets the limit of one stage, with `-` written as `_`, as in `NARWHAL_STAGE_FABRIC_EDGE_TIMEOUT_SECONDS`, and `NARWHAL_STAGE_TIMEOUT_SECONDS` sets the limit of every stage, including the [`narwhal dev` stages](../Dev-Runtime.md#stage-deadlines-and-recovery). For example, a checkpoint that takes longer than an hour to hash on an engine host needs:

```bash
NARWHAL_STAGE_DISCOVER_TIMEOUT_SECONDS=7200 narwhal deploy discover --out runs/discovery/first-deploy
```

The host runs each command under coreutils `timeout`, which sends SIGTERM to the command and every process it started at the limit, and SIGKILL 5 seconds later to a command that ignores SIGTERM. Both count as exceeding the limit. A host without `timeout` fails with `the host lacks the coreutils timeout command`. When the host has not answered 30 seconds after the limit, the workstation kills its `ssh`. The error names the host, the operation, the limit and the host's log, as in `node-2: hash checkpoint files exceeded the 3600s discover time limit; inspect runs/discovery/first-deploy/logs/node-2.log`. OpenSSH gives up on a connection after 30 seconds. `shell` and `tunnel` run until you leave them.

Each host's log holds the scripts that ran there, their exit status or time limit, and their output, at mode 0600. The log directory is `logs/` in the discovery output, the package directory or the `transfer-measure` or `fabric` output, or `runs/access-<id>/` for `access`, `tunnel`, and `shell` without `--run`.

## Fabric qualification

`transfer-measure` reads `NARWHAL_FABRIC_PROMPT_TOKENS` and the package. It refuses with exit code `2` before it reaches a host when the input is missing, when no engine of the package measures its handoffs, or when an engine has no other engine with its image, model configuration, accelerator, TP size, GPU visibility variable, runtime settings and transport to decode them. It writes `<out>/<role>/` with `before.prom`, `after.prom` and `transfer.json` for each engine, `transfer-measure.json` with each engine's decode peer, record SHA-256 and payload per handoff, and `logs/`. With `--format json`, the result's `data` holds `out` and the contents of `transfer-measure.json`.

`fabric` reads the [fabric qualification inputs](../configuration/04-Deployment-Inputs.md#fabric-qualification-inputs) and the package, and refuses with exit code `2` before it reaches a host when an input is missing or invalid, when the groups do not cover every engine role once, when a representative's `--engine-run` or `--transfer-record` is missing, or its `transfer.json` measured another prompt length or does not match the metric snapshots beside it, when a `ucx_rdma` engine names other than one HCA port, or when a `ucx_tcp` engine sends KV through another interface than `NARWHAL_FABRIC_INTERFACE`. It then runs the `fabric-budget` and `fabric-edge` stages of the [stage graph](../Deploy.md#rerunning-after-a-change) and writes each instance's evidence to `<out>/<stage>/<instance>/`, as [Qualify the transfer fabric](../deploy/04-Qualify-Fabric.md) describes:

| Path | Contents |
| --- | --- |
| `fabric-budget/<role>/` | `budget.json` of the group that `<role>` represents, and the sources it was sized from |
| `fabric-edge/<source-host>-to-<destination-host>/<sample>/` | The route, GID, link, sample and evidence files of each directed sample |
| `fabric.json` | The input digests, budgets and every sample's verdicts |
| `logs/<host-id>.log` | The scripts that ran on each host and their output |

Text mode prints one line per stage instance and sample, such as `fabric-edge node-1-to-node-2 engine-1-to-engine-2: measured 96.950 Gbit/s; engine-1 needs 0.016; passed`. A stopped stage writes `narwhal: deploy fabric: <instance>: <cause>`, for example `narwhal: deploy fabric: node-1-to-node-2 engine-1-to-engine-2: the route from 192.0.2.12 to 192.0.2.11 on node-2 leaves through eth1, not NARWHAL_FABRIC_INTERFACE eth0`. With `--format json`, the result's `data` holds `out` and the contents of `fabric.json`.

## Output and exit codes

In text mode, stdout carries progress lines, and a failure writes one stderr line, `narwhal: deploy <subcommand>: <cause>`.

| Outcome | Exit code |
| --- | :--: |
| Success | `0` |
| A remote command fails or exceeds its time limit, a fabric check fails, a fabric sample is below a role's budget, or another operating-system error | `1` |
| Invalid arguments, inventory, environment, package or discovery inputs, or an output that already exists | `2` |
| `tunnel` closed with Ctrl+C | `130` |

With `--format json`, `discover`, `access`, `package`, `install`, `transfer-measure`, `cache-groups` and `fabric` print a [versioned command result](../Command-Results.md). A failed or timed-out remote command, or a failed fabric check, exits with code `4`. Its error carries the stage, and its `context` names the host, the operation and the log, or the fabric stage instance and host. A fabric sample below a role's budget exits with code `1` and status `failed_gate`, and its `context` names the instance, the role and both rates. `shell` and `tunnel` are interactive, and refuse `--format json` with exit code `2`.

## Options

`--format` applies to `discover`, `access`, `package`, `install`, `transfer-measure`, `cache-groups` and `fabric`, and `--hosts` to every subcommand except `discover` and `cache-groups`. `--out` applies to `discover`, `package`, `transfer-measure`, `cache-groups` and `fabric`, `--run` to `install`, `transfer-measure`, `cache-groups`, `fabric` and `shell`, `--cache-layout` and `--transfer-record` to `cache-groups`, `--groups` and `--retained` to `fabric`, `--host` to `install`, `--role` to `install`, `shell` and `tunnel`, and `--forward` and `--remote-address` to `tunnel`.

| Option | Default | Description | Valid values |
| --- | --- | --- | --- |
| `--format` | `text` | Output format, either `text` or `json` for [versioned command results](../Command-Results.md). | |
| `--hosts` | `NARWHAL_HOSTS`, else `config/hosts.local.json` | Host inventory. | |
| `--out` | Required | For `discover`, `package`, `transfer-measure` and `fabric`, a new directory for the discovery output, the package, the handoff measurements or the fabric evidence, created at mode 0700. For `cache-groups`, the directory for `cache-groups.json`, created at mode 0700 when absent. | For `cache-groups`, a directory without `cache-groups.json`; otherwise a path that does not exist |
| `--cache-layout` | None | `ROLE=PATH`: the `cache-layout.json` that `narwhal-engine capture-cache` wrote for the role. Repeatable, once for each engine that captures its cache layout. | An `engine-<n>` role of the launch records |
| `--transfer-record` | None | `ROLE=PATH`: the `transfer.json` that `transfer-measure` wrote for the role, with `before.prom` and `after.prom` beside it. Repeatable, once for each engine whose handoffs are measured. | An `engine-<n>` role of the launch records |
| `--run` | Required for `install`, `transfer-measure`, `cache-groups` and `fabric`, none for `shell` | Package directory that `package` wrote. `transfer-measure`, `cache-groups` and `fabric` read the engines' packaged launch records from it, and `transfer-measure` and `fabric` their role files. | |
| `--groups` | Required | The `cache-groups.json` that `cache-groups` derived from the same package. `fabric` takes each group and its representative from it, and sizes each budget from the cache layout or transfer record the groups were derived from. | A file whose packaged launch digests and geometry-source digests still match |
| `--retained` | None | Earlier `fabric` output whose samples to reuse while their link records hold. | A directory with `fabric.json` |
| `--host` | Every inventory host | Host to install, by inventory ID. Repeatable. Excludes `--role`. | An inventory host ID |
| `--role` | Every inventory host for `install`, required for `shell`, `router` for `tunnel` | For `install`, the role whose host to install, with its colocated roles. Repeatable. Excludes `--host`. For `shell` and `tunnel`, the role whose host to open. | `router` or `engine-<n>` from the inventory |
| `--forward` | Required | `LOCAL_PORT:REMOTE_PORT`. Forwards workstation `127.0.0.1:LOCAL_PORT` to `REMOTE_PORT` on `--remote-address` at the host. Repeatable. | Ports 1 to 65535, a distinct local port per forward |
| `--remote-address` | `127.0.0.1` | Address that the forwarded ports listen on at the host. | An IPv4 or IPv6 address |
