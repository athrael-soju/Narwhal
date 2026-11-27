---
description: Subcommands, options, host-key checks, time limits and exit codes of narwhal deploy on SSH hosts and Kubernetes.
---

# `narwhal deploy`

`narwhal deploy` [deploys a fleet](../Deploy.md) to SSH hosts. `run` runs every stage from host access to a ready router and resumes at the stage that failed. Run it with `.env` loaded, from the directory that holds `config/`:

```bash
narwhal deploy plan --run runs/deploy/first-deploy
narwhal deploy run --run runs/deploy/first-deploy
narwhal deploy status --run runs/deploy/first-deploy
narwhal deploy down --run runs/deploy/first-deploy
```

With `--target kubernetes`, `plan`, `run`, `status` and `down` run the same graph on a Kubernetes cluster through `helm` and `kubectl`, as [Deploying on Kubernetes](../deploy/Kubernetes.md) describes. The other subcommands work on SSH hosts only.

The other subcommands run single steps by hand: discovery, the access check, packaging, installation, SGLang handoff measurement, the cache-equivalence groups and fabric qualification. They also open role shells and port tunnels:

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
| `plan` | Checks every input and each host's systemd user manager, then prints each stage instance up to `--until` with whether `run` would run it, and why. Reaches each host once, and runs no stage. | [Running the stage graph](#running-the-stage-graph) |
| `run` | Runs every stage up to `--until`, skipping each stage whose recorded result still holds, and stops at the first stage that fails. | [Running the stage graph](#running-the-stage-graph) |
| `status` | Prints each stage's state, attempt and evidence directory from the run record, with the recovery section of a failed stage, then checks each engine, sidecar, router and fabric route the run recorded. | [Running the stage graph](#running-the-stage-graph) |
| `down` | Stops the router, then the sidecars, then the engine containers the run started, each only while its recorded identity still matches. | [Running the stage graph](#running-the-stage-graph) |
| `discover` | Logs in to every host that the `NARWHAL_NODE_<n>_SSH` and `NARWHAL_ROUTER_SSH` destinations name, inspects the GPUs, the engine image and the checkpoint on each engine host, checks each checkpoint against `NARWHAL_MODEL_TREE_SHA256`, and writes the host inventory, launch records, fleet and `deployment.env`. | [Running discovery and access checks](../deploy/01-Discover.md#running-discovery-and-access-checks) |
| `access` | Checks the inventory, then logs in once to each host and records its `hostname` output. Prints each host ID with its roles. | [Running discovery and access checks](../deploy/01-Discover.md#running-discovery-and-access-checks) |
| `package` | Checks the release wheel that `NARWHAL_WHEEL` names and the dependency lock that `NARWHAL_REQUIREMENTS` names against their SHA-256 values, then writes them, the helper snapshots from it and each host's role files into a new directory with a private manifest. | [Building the deployment package](../deploy/02-Install.md#building-the-deployment-package) |
| `install` | Copies a package to each selected host, checking the SHA-256 of every file, and installs the locked dependencies and the wheel into a virtual environment in the host's deployment directory. | [Installing engine 1 and the remaining hosts](../deploy/02-Install.md#installing-engine-1-and-the-remaining-hosts) |
| `transfer-measure` | Measures eight KV handoffs from every engine whose backend sizes its fabric budget from them, on SGLang, to the next engine role with the same launch inputs, and writes each engine's `transfer.json`. | [Measuring KV handoffs](../deploy/04-Qualify-Fabric.md#measuring-kv-handoffs) |
| `cache-groups` | Groups the engine roles of the launch records that `NARWHAL_LAUNCH_CONFIG` names, else `config/engine-launch.local.json`, by their discovered image and model configuration, their allocation, runtime and transport, and each engine's captured cache layout or measured KV handoff, and writes `cache-groups.json`. Prints each group's ID, representative and roles. | [Deriving cache-equivalence groups](../deploy/03-Validate-Engines.md#deriving-cache-equivalence-groups) |
| `fabric` | Sizes each cache group's KV budget, then checks the tools, routes, test port and RDMA GID entries of every directed edge between engine hosts and measures it with `iperf3` or `ib_write_bw`, or reuses a retained sample whose link record is unchanged, against the budget of every source role. | [Qualify the transfer fabric](../deploy/04-Qualify-Fabric.md) |
| `shell` | Opens an interactive login shell on a role's host. With `--run`, the shell starts in the deployment directory with the role environment loaded and `.venv` active. | [Opening installed role shells](../deploy/02-Install.md#opening-installed-role-shells) |
| `tunnel` | Forwards workstation loopback ports to a role's host until you press Ctrl+C. | [Tunnelling router, Prometheus, and Grafana to the workstation](../deploy/07-Serve-and-Measure.md#tunnelling-router-prometheus-and-grafana-to-the-workstation) |

`access`, `package`, `install`, `transfer-measure`, `fabric`, `shell` and `tunnel` read the inventory that discovery writes. `plan`, `run`, `status` and `down` derive the hosts from the `NARWHAL_NODE_<n>_SSH` and `NARWHAL_ROUTER_SSH` destinations, as `discover` does. [Host inventory and SSH trust](../configuration/04-Deployment-Inputs.md#host-inventory-and-ssh-trust) describes it.

## Running the stage graph

`run` keeps a [run record](../telemetry/06-Deployment-Runs.md), `run.json`, in the directory `--run` names, and writes each stage attempt to `stages/<stage>/<instance>/attempt-<n>/` beside it. On a host, the attempt works in `runs/deploy/<run-id>/stages/<stage>/<instance>/attempt-<n>/` under the deployment directory, and its files come back to the workstation attempt. The [stage table](../Deploy.md#rerunning-after-a-change) lists what reruns each stage.

`plan` and `run` check every input before they reach a host, and exit with code `2` naming each one that is missing or malformed: the discovery, fabric and package inputs and the [deployment run inputs](../configuration/04-Deployment-Inputs.md#deployment-run-inputs). `plan` then checks that each host's systemd user manager can run the sidecar and router units, and exits with code `1` naming each host that cannot. It reads the run record without writing it and checks the recorded processes and routes read-only. A stage after one that would rerun shows `runs after <stage>, whose output changed` when that stage's files would change, and `runs after <stage> if <fact> changes` when that stage's new result decides, as a new engine or unit identity does. A unit that systemd restarted is planned as if its check passes, since that check writes evidence on the host, and `plan` prints a `Note:` line naming the stage that reruns if it fails.

Each stage runs on its hosts:

| Stage | What `run` does |
| --- | --- |
| `access` | Logs in to the host and keeps its `hostname` output. |
| `discover` | Runs discovery into the attempt and writes `hosts.local.json`, `fleet.local.json`, `engine-launch.local.json` and `deployment.env` there. Later stages read these instead of `config/`. |
| `package`, `install` | Builds the package from the discovered files and installs it on each host. |
| `engine-inspect`, `engine-prepare`, `engine-check`, `engine-probe`, `cache-capture` | Runs `narwhal-engine inspect-host`, `prepare`, `check`, `probe` or `capture-cache` for the engine. `engine-prepare` writes a new launch directory in its attempt. Before `engine-inspect` runs again, the run stops its own sidecar and engine container of that role, whose ports the inspection checks. |
| `engine-start` | Removes the container of the engine's previous start, then runs `narwhal-engine start --ready-seconds` with the stage limit less 60 seconds. A container that fails to answer is removed after its logs are kept. A launch directory starts one engine: when `engine-start` reruns on a directory that already started one, it stops nothing, `engine-prepare` writes a fresh directory and the run continues from there. |
| `transfer-measure`, `cache-groups`, `fabric-budget`, `fabric-edge` | Measures the engine's KV handoffs, derives the groups and qualifies each edge as the matching subcommand does. `fabric-edge` reuses the previous attempt's sample while its link record holds. |
| `fleet-inventory` | Checks that the router's installed fleet names every engine of the inventory and that its role environment defines every `${...}` reference of the fleet. |
| `attest-capture`, `sidecar-verify`, `finalize-fleet` | Captures and publishes the engine's attestation, verifies its sidecar, and writes the attested `engine_contract` into the router's fleet. |
| `sidecar-start`, `router-start` | Starts the attestation sidecar or `narwhal-serve` as a systemd user unit of the run, in place of the previous attempt's unit, and waits for `/health`, and for the router `/ready`. `router-start` keeps the router's health, readiness, state and metrics responses and one completion. |
| `profile`, `calibrate`, `preflight` | Runs `narwhal-profile`, `narwhal-check --calibrate-first-token` or `narwhal-check` on the router host with the fleet that `fleet-limits` or the stage before it wrote. |
| `fleet-limits` | Writes the SLO, `engine.first_token_timeout_s` as the calibrated candidate plus `NARWHAL_FIRST_TOKEN_MARGIN_S`, and the calibration path into the router's fleet. |

When `router-start` reruns only because an engine restarted, and the router still runs on an unchanged fleet, it keeps the router and waits up to 120 seconds for the router to serve each restarted engine again. The router finds the new process itself, as [Detect process replacement](../operate/03-Restart-Engines.md#detect-process-replacement) describes, and its recovery returns the engine to placement. An engine the router holds `blocked` or `drained` is readmitted with its own `POST /narwhal/lifecycle/readmit`. An engine counts as back once it is `active`, accepts new requests, and the router's accepted process start differs from the one it held before the restart. A refused readmission, or an active whole-wave restart, fails the stage with the router's reason.

`run` and `status` check each recorded engine container, sidecar unit, router unit and fabric route first. A unit that systemd restarted after its process failed has a new identity. When its sidecar verification or router health check passes, the run records the new identity in place and reruns only the stages after it. When the check fails, its stage reruns. `status` takes the run's lock to record it, and exits with code `3` when a recorded process is gone or changed, or a route changed. While another runner holds the run, `status` reads the record and each process without running the check, records nothing, reports a restarted unit as `restarted, not checked`, and sets `locked` in its JSON data. `run` and `down` exit with code `4` instead.

A failed stage prints its cause, its recovery section and its evidence directory:

```text
narwhal: deploy run: engine-check[engine-1] failed: node-1: check the engine-1 launch exited 1: ...
Recovery: https://athrael-soju.github.io/Narwhal/deploy/03-Validate-Engines/#preparing-checking-and-starting-each-engine
Evidence: runs/deploy/first-deploy/stages/engine-check/engine-1/attempt-1
```

Fix the cause and run `run` again. It resumes at the failed stage. `down` stops each process only while its recorded identity matches, so it never stops a process another run started.

Only container engines run on SSH hosts, and `run` starts one router. The `monitoring` and `trial` stages run by hand, as [Serve and measure](../deploy/07-Serve-and-Measure.md) describes.

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
| `engine-inspect`, `engine-prepare`, `engine-probe`, `fleet-limits`, `sidecar-start`, `sidecar-verify`, `finalize-fleet`, `down` | Each host step of the stage | 300 s |
| `engine-check`, `engine-start` | The image and checkpoint check, or the start and its wait for `/health` | 1,800 s |
| `cache-capture`, `router-start` | The cache layout capture, or the router start and its checks | 600 s |
| `fleet-inventory` | Reading the installed fleet and role environment | 60 s |
| `attest-capture` | The attestation capture and publication | 900 s |
| `preflight` | `narwhal-check` | 3,600 s |
| `profile`, `calibrate` | `narwhal-profile`, or the first-token calibration | 7,200 s |

`NARWHAL_STAGE_<STAGE>_TIMEOUT_SECONDS` sets the limit of one stage, with `-` written as `_`, as in `NARWHAL_STAGE_FABRIC_EDGE_TIMEOUT_SECONDS`, and `NARWHAL_STAGE_TIMEOUT_SECONDS` sets the limit of every stage, including the [`narwhal dev` stages](../Dev-Runtime.md#stage-deadlines-and-recovery). For example, a checkpoint that takes longer than an hour to hash on an engine host needs:

```bash
NARWHAL_STAGE_DISCOVER_TIMEOUT_SECONDS=7200 narwhal deploy discover --out runs/discovery/first-deploy
```

The host runs each command under coreutils `timeout`, which sends SIGTERM to the command and every process it started at the limit, and SIGKILL 5 seconds later to a command that ignores SIGTERM. Both count as exceeding the limit. A host without `timeout` fails with `the host lacks the coreutils timeout command`. When the host has not answered 30 seconds after the limit, the workstation kills its `ssh`. The error names the host, the operation, the limit and the host's log, as in `node-2: hash checkpoint files exceeded the 3600s discover time limit; inspect runs/discovery/first-deploy/logs/node-2.log`. OpenSSH gives up on a connection after 30 seconds. `shell` and `tunnel` run until you leave them.

Each host's log holds the scripts that ran there, their exit status or time limit, and their output, at mode 0600. The log directory is `logs/` in the discovery output, the package directory or the `transfer-measure` or `fabric` output, `logs/` in each stage attempt of `run`, `live/` in the run directory for the checks of `run`, `status` and `down`, or `runs/access-<id>/` for `access`, `plan`, `tunnel`, and `shell` without `--run`.

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
| A remote command fails or exceeds its time limit, a fabric check fails, a fabric sample is below a role's budget, a stage's check rejects it, a host cannot run units for `plan`, or another operating-system error | `1` |
| Invalid arguments, inventory, environment, package or discovery inputs, or an output that already exists | `2` |
| `status` finds a recorded process gone or changed, or a route changed | `3` |
| A stage of `run` fails without a verdict or exceeds its time limit, or another runner holds the run for `run` or `down` | `4` |
| `run` or `tunnel` stopped with Ctrl+C | `130` |

With `--format json`, `plan`, `run`, `status`, `down`, `discover`, `access`, `package`, `install`, `transfer-measure`, `cache-groups` and `fabric` print a [versioned command result](../Command-Results.md). A stage of `run` that fails exits with status `failed_gate` and code `1` when its host command exited 1, the command result contract's rejected gate, or its check rejected it. Any other stage failure exits with status `error` and code `4`, and a stage past its time limit with error code `stage_timeout`. The error carries the stage instance, and its `context` holds the host, exit status and log, the run record status and the `recovery` URL. Each missing or malformed input is one `invalid_input` error. A failed or timed-out remote command, or a failed fabric check, exits with code `4`. Its error carries the stage, and its `context` names the host, the operation and the log, or the fabric stage instance and host. A fabric sample below a role's budget exits with code `1` and status `failed_gate`, and its `context` names the instance, the role and both rates. `shell` and `tunnel` are interactive, and refuse `--format json` with exit code `2`.

## Options

`--format` applies to every subcommand except `shell` and `tunnel`, and `--hosts` to every subcommand except `plan`, `run`, `status`, `down`, `discover` and `cache-groups`. `--out` applies to `discover`, `package`, `transfer-measure`, `cache-groups` and `fabric`, `--run` to `plan`, `run`, `status`, `down`, `install`, `transfer-measure`, `cache-groups`, `fabric` and `shell`, `--until` to `plan` and `run`, `--cache-layout` and `--transfer-record` to `cache-groups`, `--groups` and `--retained` to `fabric`, `--host` to `install`, `--role` to `install`, `shell` and `tunnel`, and `--forward` and `--remote-address` to `tunnel`.

| Option | Default | Description | Valid values |
| --- | --- | --- | --- |
| `--format` | `text` | Output format, either `text` or `json` for [versioned command results](../Command-Results.md). | |
| `--hosts` | `NARWHAL_HOSTS`, else `config/hosts.local.json` | Host inventory. | |
| `--out` | Required | For `discover`, `package`, `transfer-measure` and `fabric`, a new directory for the discovery output, the package, the handoff measurements or the fabric evidence, created at mode 0700. For `cache-groups`, the directory for `cache-groups.json`, created at mode 0700 when absent. | For `cache-groups`, a directory without `cache-groups.json`; otherwise a path that does not exist |
| `--cache-layout` | None | `ROLE=PATH`: the `cache-layout.json` that `narwhal-engine capture-cache` wrote for the role. Repeatable, once for each engine that captures its cache layout. | An `engine-<n>` role of the launch records |
| `--transfer-record` | None | `ROLE=PATH`: the `transfer.json` that `transfer-measure` wrote for the role, with `before.prom` and `after.prom` beside it. Repeatable, once for each engine whose handoffs are measured. | An `engine-<n>` role of the launch records |
| `--run` | Required for every subcommand that takes it except `shell`, none for `shell` | For `plan`, `run`, `status` and `down`, the run directory that holds the run record; `run` creates it at mode 0700, and `run`, `status` and `down` refuse one another user owns or may enter. For the others, the package directory that `package` wrote. `transfer-measure`, `cache-groups` and `fabric` read the engines' packaged launch records from it, and `transfer-measure` and `fabric` their role files. | |
| `--target` | `ssh` | Where `plan`, `run`, `status` and `down` run the fleet: on SSH hosts, or on a Kubernetes cluster through `helm` and `kubectl`. A run directory keeps the target it started with. | `ssh` or `kubernetes` |
| `--until` | `router-start` | The last stage `plan` decides or `run` runs. | A stage of the [stage table](../Deploy.md#rerunning-after-a-change) up to `router-start` |
| `--groups` | Required | The `cache-groups.json` that `cache-groups` derived from the same package. `fabric` takes each group and its representative from it, and sizes each budget from the cache layout or transfer record the groups were derived from. | A file whose packaged launch digests and geometry-source digests still match |
| `--retained` | None | Earlier `fabric` output whose samples to reuse while their link records hold. | A directory with `fabric.json` |
| `--host` | Every inventory host | Host to install, by inventory ID. Repeatable. Excludes `--role`. | An inventory host ID |
| `--role` | Every inventory host for `install`, required for `shell`, `router` for `tunnel` | For `install`, the role whose host to install, with its colocated roles. Repeatable. Excludes `--host`. For `shell` and `tunnel`, the role whose host to open. | `router` or `engine-<n>` from the inventory |
| `--forward` | Required | `LOCAL_PORT:REMOTE_PORT`. Forwards workstation `127.0.0.1:LOCAL_PORT` to `REMOTE_PORT` on `--remote-address` at the host. Repeatable. | Ports 1 to 65535, a distinct local port per forward |
| `--remote-address` | `127.0.0.1` | Address that the forwarded ports listen on at the host. | An IPv4 or IPv6 address |
