---
description: Deploy a Narwhal fleet of vLLM, SGLang or TensorRT-LLM engines on SSH hosts with narwhal deploy, from host discovery to a serving router or router pair.
---

# Deploying a fleet

`narwhal deploy run` takes engine hosts that hold a staged checkpoint and engine image to a serving fleet: the engines checked, started and attested, the fabric qualified, the engines profiled and calibrated, preflight passed, and one router, or a router pair behind HAProxy, with the monitoring stack running. It runs from a management workstation, needs no shell on a host and asks no questions. [Getting started](deploy/Getting-Started.md) takes a first fleet through it on one host or several.

With the inputs in the environment, from the directory that holds the run directories:

```bash
narwhal deploy plan --run runs/deploy/first-deploy
narwhal deploy run --run runs/deploy/first-deploy
narwhal deploy status --run runs/deploy/first-deploy
narwhal deploy down --run runs/deploy/first-deploy
```

`plan` checks every input before it touches a host, then checks each host's systemd user manager and lingering, and Docker Compose on the primary router's host. It prints each stage with whether `run` would run it and why. `run` runs every stage of the [stage table](#rerunning-after-a-change) in order, records each attempt in the run directory, and stops at the first stage that fails, printing its cause, its recovery section and its evidence directory. Fix the cause and run `run` again: it resumes at that stage and skips every stage whose inputs, outputs and live processes still verify. `status` reports each stage and checks every process the run started, and `down` stops only those processes. [`narwhal deploy`](cli/Deploy.md) lists every option and exit code.

With `--trial`, `run` continues past the monitoring stack with the [capacity trial](deploy/07-Serve-and-Measure.md#running-the-initial-capacity-trial) of the workload and thresholds in the trial file:

```bash
narwhal deploy run --run runs/deploy/first-deploy --trial config/capacity-trial.json
```

On Kubernetes, a `NarwhalFleet` resource holds the inputs and a fleet controller in the cluster runs the stages, as [Deploying on Kubernetes](deploy/Kubernetes.md) describes. `narwhal deploy --target kubernetes` applies that resource server-side from the workstation, unless another field manager owns its spec, follows its status, and copies the evidence back through export Jobs the controller makes on the workstation's request.

## Requirements

Deploy one model per fleet. Its engines need a compatible KV layout, KV transfer between every eligible producer and consumer, and one active role controller. vLLM transfers KV through NIXL `kv_both`. SGLang transfers KV through the `mooncake` connector. TensorRT-LLM transfers KV through its C++ cache transceiver, over UCX or NIXL. `narwhal deploy` runs container engines.

| Host | What `narwhal deploy` runs there | What the host needs |
| --- | --- | --- |
| Management workstation | `narwhal deploy` itself, the run directory, and the capacity trial's load client | Python 3.11 or later with Narwhal installed, OpenSSH 8.5 or later, and `uv` to compile the dependency lock. The private `.env`, the known-hosts file, the release wheel and its lock |
| Router host | The router as a systemd user unit. On the primary router's host, Prometheus and Grafana, and HAProxy unless the load balancer has a host of its own | Python 3.11 or later with `venv` and access to a Python package index, coreutils, systemd 240 or later with a user manager and lingering for the SSH user, and Docker Engine with Compose v2 on the primary router's host. For a router pair, Docker on the load balancer's host and the lease directory mounted on both router hosts |
| Engine host | The engine containers, and their attestation sidecars as systemd user units | Python 3.11 or later with `venv` and access to a Python package index, coreutils, systemd 240 or later with a user manager and lingering for the SSH user, Docker usable by the SSH user, the accelerator driver and container runtime, `ip`, `rocminfo` or `nvidia-smi`, `iperf3` or `perftest`, `util-linux`, and the transfer devices. The pinned engine image and the staged checkpoint |

One host can hold every role. Preparing a host needs root, which `narwhal deploy` never uses; [Preparing SSH hosts](deploy/Getting-Started.md#preparing-ssh-hosts) lists what to prepare.

### Deployment record

The run directory is the deployment record. `run.json` records each stage instance's latest attempt with its inputs as digests, the SHA-256 of every file it wrote and the identity of every process it started. Earlier attempts' directories stay under `stages/`, and each attempt's directory holds its evidence and the log of every command it ran on a host. Archive the run directory with the private `.env` and the known-hosts file. Replace private addresses, paths, and credentials with stable aliases before exporting evidence.

## Deployment sequence

The stages fall into seven gates. Each gate page lists its stages with what they run, their inputs, their evidence and their recovery, and the command that runs each stage alone.

<div class="grid cards" markdown>

-   [Discover the deployment](deploy/01-Discover.md)

    ---

    `access` and `discover`: log in to every host and inspect the engine hosts.

-   [Install Narwhal on the hosts](deploy/02-Install.md)

    ---

    `package` and `install`: check the release wheel and install it on every host.

-   [Validate and start engines](deploy/03-Validate-Engines.md)

    ---

    Inspect each engine host, then prepare, check, start and probe each engine.

-   [Qualify the transfer fabric](deploy/04-Qualify-Fabric.md)

    ---

    Measure every directed edge between engine hosts against its KV budget.

-   [Attest live engines](deploy/05-Attest.md)

    ---

    Capture, serve and verify each engine's attestation, and finalize the fleet contract.

-   [Profile engines and run preflight](deploy/06-Profile-and-Preflight.md)

    ---

    Profile, calibrate the first-token deadline, set the SLOs and run preflight.

-   [Serve and measure](deploy/07-Serve-and-Measure.md)

    ---

    Start the routers, HAProxy and monitoring, and run the capacity trial.

</div>

After the capacity trial passes, run [advisory mode](configuration/02-Serving-and-Role-Control.md#advisory-rollout) on representative traffic before you allow production role movement.

### Running one stage alone

`run --until <stage>` stops after that stage. Stages whose result holds are skipped, so on a run that passed up to there, it runs only the stages that changed. Each gate page also gives the command a stage runs, which you can run alone, usually in the [role shell](deploy/02-Install.md#opening-installed-role-shells) of the stage's host. A command run alone writes where you point it and changes nothing in the run record.

### Rerunning after a change

Each stage in the first table runs one section of a gate page. The stage runner keeps a [run record](telemetry/06-Deployment-Runs.md) of every stage: its input digests and the SHA-256 of each file it wrote. On the next run it skips a stage whose last attempt passed, whose digests still match and whose files are unchanged. Engine, sidecar and router processes and fabric routes are checked live first, and a stage whose process is gone runs again.

A change reruns the stages its row lists and no others. A change that touches several rows reruns all of them: an engine restart that changes the engine's attested binding digest reruns the engine process row and the binding digest row. A changed output file reruns its stage and every stage after it.

An engine whose container stopped or restarted also spends its launch directory, which starts one engine. `engine-prepare` writes a fresh directory, then `engine-check` and `engine-start` run from it, and the engine process row follows. A fresh directory of an unchanged launch record attests the same planned launch, so the binding digest row adds its stages only when the live captures differ.

<!-- Generated from src/narwhal/deployment/deploy_graph.py by tools/maintenance/stage_graph_doc.py. Edit the graph, then run the tool. -->

| Stage | Runs | After | Gate section |
| --- | --- | --- | --- |
| `access` | Once for each host in the inventory |  | [Running discovery and access checks](deploy/01-Discover.md#running-discovery-and-access-checks) |
| `discover` | Once per run, on the management workstation | `access` | [Running discovery and access checks](deploy/01-Discover.md#running-discovery-and-access-checks) |
| `package` | Once per run, on the management workstation | `discover` | [Building the deployment package](deploy/02-Install.md#building-the-deployment-package) |
| `install` | Once for each host in the inventory | `package` | [Installing Narwhal on each host](deploy/02-Install.md#installing-narwhal-on-each-host) |
| `engine-inspect` | Once for each engine role, on its engine host | `install` | [Inspecting every engine host](deploy/03-Validate-Engines.md#inspecting-every-engine-host) |
| `engine-prepare` | Once for each engine role, on its engine host | `engine-inspect` | [Preparing, checking, and starting each engine](deploy/03-Validate-Engines.md#preparing-checking-and-starting-each-engine) |
| `engine-check` | Once for each engine role, on its engine host | `engine-prepare` | [Preparing, checking, and starting each engine](deploy/03-Validate-Engines.md#preparing-checking-and-starting-each-engine) |
| `engine-start` | Once for each engine role, on its engine host | `engine-check` | [Preparing, checking, and starting each engine](deploy/03-Validate-Engines.md#preparing-checking-and-starting-each-engine) |
| `engine-probe` | Once for each engine role, on its engine host | `engine-start` | [Proving the live HTTP process](deploy/03-Validate-Engines.md#proving-the-live-http-process) |
| `cache-capture` | Once for each engine role, on its engine host, on TensorRT-LLM or vLLM | `engine-probe` | [Capturing the live cache layout](deploy/03-Validate-Engines.md#capturing-the-live-cache-layout) |
| `transfer-measure` | Once for each engine role, on its engine host, on SGLang | `engine-probe` | [Measuring KV handoffs](deploy/04-Qualify-Fabric.md#measuring-kv-handoffs) |
| `cache-groups` | Once per run, on the management workstation | `discover`, `cache-capture`, `transfer-measure` | [Deriving cache-equivalence groups](deploy/03-Validate-Engines.md#deriving-cache-equivalence-groups) |
| `fabric-budget` | Once for each cache-equivalence group | `cache-groups` | [Building the source budget](deploy/04-Qualify-Fabric.md#building-the-source-budget) |
| `fabric-edge` | Once for each directed edge between engine hosts | `fabric-budget` | [Binding each sample to a directed route](deploy/04-Qualify-Fabric.md#binding-each-sample-to-a-directed-route) |
| `fleet-inventory` | Once for each router host | `install` | [Checking the router inventory](deploy/05-Attest.md#checking-the-router-inventory) |
| `attest-capture` | Once for each engine role, on its engine host | `fabric-edge`, `fleet-inventory` | [Capturing the attestation](deploy/05-Attest.md#capturing-the-attestation) |
| `sidecar-start` | Once for each engine role, on its engine host | `attest-capture` | [Starting the sidecar](deploy/05-Attest.md#starting-the-sidecar) |
| `sidecar-verify` | Once for each engine role, on its engine host | `sidecar-start` | [Verifying the sidecar](deploy/05-Attest.md#verifying-the-sidecar) |
| `finalize-fleet` | Once for each router host | `sidecar-verify` | [Finalizing the fleet contract](deploy/05-Attest.md#finalizing-the-fleet-contract) |
| `profile` | Once per run, on the management workstation | `finalize-fleet` | [Profiling idle engines](deploy/06-Profile-and-Preflight.md#profiling-idle-engines) |
| `calibrate` | Once per run, on the management workstation | `profile` | [Calibrating the first-token deadline](deploy/06-Profile-and-Preflight.md#calibrating-the-first-token-deadline) |
| `fleet-limits` | Once per run, on the management workstation | `calibrate` | [Reusing or repeating the calibration](deploy/06-Profile-and-Preflight.md#reusing-or-repeating-the-calibration) |
| `preflight` | Once for each router host | `fleet-limits` | [Running preflight](deploy/06-Profile-and-Preflight.md#running-preflight) |
| `lease-check` | Once per run, on the management workstation, on fleets with a standby router | `install` | [Checking the shared lease path](deploy/07-Serve-and-Measure.md#checking-the-shared-lease-path) |
| `router-start` | Once for each router host | `preflight`, `lease-check` | [Starting the router pair](deploy/07-Serve-and-Measure.md#starting-the-router-pair) |
| `load-balancer` | Once per run, on the management workstation, on fleets with a standby router | `router-start` | [Starting the load balancer](deploy/07-Serve-and-Measure.md#starting-the-load-balancer) |
| `monitoring` | Once per run, on the management workstation | `load-balancer` | [Starting the monitoring stack](deploy/07-Serve-and-Measure.md#starting-the-monitoring-stack) |
| `trial` | Once per run, on the management workstation | `monitoring` | [Running the initial capacity trial](deploy/07-Serve-and-Measure.md#running-the-initial-capacity-trial) |

| Change | Recorded as | Reruns |
| --- | --- | --- |
| SSH credentials, jump host or expected host keys | The password variable names, `NARWHAL_SSH_JUMP` and the SHA-256 of `NARWHAL_SSH_KNOWN_HOSTS` | `access` |
| SSH destination or roles of a host | Every host's SSH destination and roles | `access`, `package` and every stage after it |
| Hardware, model, image, checkpoint, launch policy, route or transport | The discovery fields of `.env`, the live image ID and the checkpoint tree digest | `discover` and every stage after it |
| Approved release | `NARWHAL_WHEEL_SHA256`, which each role environment records as `NARWHAL_DEPLOYMENT_REVISION`, and the SHA-256 of the lock, `NARWHAL_REQUIREMENTS_SHA256` | `package` and every stage after it |
| SLO targets or router overhead | `slo.ttft_s`, `slo.tpot_s` and `slo.router_overhead_s` | `fleet-limits`, `preflight`, `router-start`, `trial` |
| First-token deadline margin | The margin above the calibrated first-token candidate | `fleet-limits`, `preflight`, `router-start`, `trial` |
| Profiling sweep or warmup | The prefill and decode sweep lengths, the decode concurrency and the warmup requests | `profile`, `fleet-limits`, `preflight`, `router-start`, `trial` |
| First-token calibration inputs | The input tokens, samples and observation timeout | `calibrate`, `fleet-limits`, `preflight`, `router-start`, `trial` |
| Fabric budget workload | The prompt tokens, handoffs per second, burst, transfer budget and headroom | `transfer-measure` on SGLang when the prompt tokens change, `fabric-budget`, `fabric-edge` |
| Fabric test settings | The test port, and each engine's RDMA GID index and RoCE mode | `fabric-edge` |
| Router pair settings | Both routers' origins, the load balancer's origin, the lease path and the HAProxy image | `lease-check` on fleets with a standby router, `router-start`, `load-balancer` on fleets with a standby router, `monitoring`, `trial` |
| Monitoring settings | The Prometheus and Grafana listen addresses | `monitoring` |
| Trial workload, rates, request count or thresholds | The SHA-256 of the `--trial` file | `trial` from a drained router |
| Launch directory whose engine has stopped | The launch directory and the SHA-256 of its `launch.json`, checked live on every run | `engine-check`, `engine-start` for that engine |
| Engine process restarted | The container ID and start time, or the PID, boot ID and start ticks, checked live on every run | `engine-probe`, `cache-capture` on TensorRT-LLM or vLLM, `transfer-measure` on SGLang, `attest-capture`, `sidecar-start`, `sidecar-verify` for that engine; `finalize-fleet`, `preflight`, `router-start` to readmit the engine to a router that still runs |
| Engine cache geometry | The SHA-256 of `cache-layout.json` without `plan_sha256`, which names its launch directory's plan | `cache-groups`; `fabric-budget` for the engine's cache group; `fabric-edge` for every edge leaving a host of the engine's group |
| Measured KV handoff size of an engine | The SHA-256 of the engine's `transfer.json` handoff measurement | `cache-groups`; `fabric-budget` for the engine's cache group; `fabric-edge` for every edge leaving a host of the engine's group |
| Fabric route of a directed edge | The link fingerprint: roles, addresses, interfaces, both live routes, transport, tool version and test parameters, checked live on every run | `fabric-edge` for that edge; `calibrate`, `fleet-limits`, `preflight`, `router-start`, `trial` |
| Attested binding digest of an engine | The `launch_digest`, or the `attestation_digest` when the attestation has no launch evidence | `profile`, `calibrate`, `fleet-limits`, `preflight`, `router-start`, `trial` |
| Engine contract fields of an engine | The `EngineContract` fields of its attestation | `attest-capture` for every engine; `finalize-fleet`, `profile`, `calibrate`, `fleet-limits`, `preflight`, `router-start`, `trial` |
| Attestation sidecar restarted | The sidecar's recorded process identity, checked live on every run | `sidecar-start`, `sidecar-verify` for that engine; `finalize-fleet` |
| Router restarted | The router's recorded process identity, checked live on every run | `router-start` for that router host |
| Load balancer restarted | The HAProxy container ID and start time, checked live on every run | `load-balancer` on fleets with a standby router |
| Monitoring container stopped or replaced | The container ID of each monitoring service, checked live on every run | `monitoring` |

<!-- End of the generated stage tables. -->

## Evidence and recovery

Each attempt writes its evidence to `stages/<stage>/<instance>/attempt-<n>/` in the run directory, or `stages/<stage>/attempt-<n>/` for a stage that runs once per run, with the log of every command it ran on each host in `logs/<host-id>.log`. On a host, the attempt works in `runs/deploy/<run-id>/` of the same path under the deployment directory, and the files it names come back to the workstation, a failed attempt's included. `narwhal deploy status` prints each stage's evidence directory, and each gate page lists the files.

A failed stage prints the link to its section, whose recovery applies:

- Discovery and access: [Access failure handling](deploy/01-Discover.md#access-failure-handling).
- Package and install: [Recovering a failed installation](deploy/02-Install.md#recovering-a-failed-installation).
- Engines: the section of the failed stage in [Validate and start engines](deploy/03-Validate-Engines.md).
- Fabric: [Troubleshooting an edge below its budget](deploy/04-Qualify-Fabric.md#troubleshooting-an-edge-below-its-budget).
- Attestation: when `capture` fails, fix the cause it names, as in [Capturing the attestation](deploy/05-Attest.md#capturing-the-attestation). When `verify` or `finalize-fleet` fails, fix the failing engine. Then run again.
- Profiling: [Reusing or repeating profiles](deploy/06-Profile-and-Preflight.md#reusing-or-repeating-profiles).
- Preflight: follow the reported engine, transfer leg or budget into [Troubleshooting a fleet](Troubleshoot.md).
- Routers: [Checking the shared lease path](deploy/07-Serve-and-Measure.md#checking-the-shared-lease-path) and [Starting the router pair](deploy/07-Serve-and-Measure.md#starting-the-router-pair).
- Capacity trial: [Running the initial capacity trial](deploy/07-Serve-and-Measure.md#running-the-initial-capacity-trial).

## After a host reboot

`narwhal deploy` does not bring a fleet back by itself after a host reboots. The sidecar and router units are transient, so they do not survive the reboot, and the engine containers have no restart policy, so they stay stopped. HAProxy runs with Docker's `on-failure` policy and may stay stopped too. The monitoring containers run with the `always` policy, so Docker starts them again when it starts. The lease directory must come back from the host's own mount configuration, such as `/etc/fstab`.

While an engine host is down, `NarwhalHostDown` fires, and while the standby router's host is down, `NarwhalStandbyRouterDown`, as [Monitor](operate/02-Monitor.md) lists. Prometheus runs on the primary router's host, so no rule fires while that host is down: route `NarwhalWatchdog` to a dead man's switch outside the fleet. Once the hosts are back, run `narwhal deploy run` again. It finds each recorded process gone, prepares and starts the engines from fresh launch directories, captures and verifies their attestations, starts the sidecars, finalizes the fleet, runs preflight and starts the routers. It profiles and calibrates again only when an engine's attested binding digest changed. `router-start` refuses a host whose lease directory came back without the shared mount.

## Deploying a new release

`narwhal deploy` has no rolling upgrade. A new release is a new `NARWHAL_WHEEL` and `NARWHAL_WHEEL_SHA256`, and its lock, and the next `run` repeats `package` and every stage after it: it installs the new wheel on every host, then inspects, prepares, starts and attests every engine again, profiles, calibrates and restarts the routers. `engine-inspect` stops the running engines and sidecars first, so the fleet serves nothing until `router-start` passes. [Upgrade and rollback](operate/04-Upgrade-and-Validate.md) covers the router pair.

## Where each process logs

| Process | Log |
| --- | --- |
| `narwhal deploy` | Its own output, and in the run directory each attempt's `logs/<host-id>.log` with the scripts it ran on each host, their exit status and output. `live/` holds the logs of the process checks of `run`, `status` and `down` |
| Engine container | `docker logs <container>` on the engine host, with the container ID in the `engine-start` attempt's `container.id`. A start that fails keeps the last 200 lines in the attempt's `engine.log` |
| Attestation sidecar | `sidecar-<n>.log` in the `sidecar-start` attempt directory on the engine host, which keeps the output of every automatic restart |
| Router | `router.log` and the request journal `journal.jsonl` in the `router-start` attempt directory on the router host |
| HAProxy | `docker logs narwhal-<run-id>-load-balancer` on the load balancer's host |
| Prometheus and Grafana | `docker logs` of each container of the Compose project `observability` on the primary router's host |

The systemd journal records each start, stop and automatic restart of a unit, `narwhal-<run-id>-router.service` or `narwhal-<run-id>-sidecar-<n>.service`. Read it on the host with `journalctl --user -u <unit>`. The unit's own output goes to its log file, not the journal. The run ID is the run record's `run_id`.

## Runtime and tooling references

<div class="grid cards" markdown>

-   [Configuration](Configuration.md)

    ---

    Fleet fields and defaults.

-   [Measuring a fleet](Measure.md)

    ---

    Profiling and service-level objective (SLO) calibration.

-   [Setting up observability](Observability.md)

    ---

    Prometheus and Grafana setup.

-   [Troubleshooting a fleet](Troubleshoot.md)

    ---

    Symptom-based diagnosis.

</div>

