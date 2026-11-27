---
description: Deploy Narwhal on a multi-node vLLM, SGLang or TensorRT-LLM fleet, from host discovery to live serving.
---

# Deploying a fleet

## Requirements

Deploy one model per fleet. Its engines need a compatible KV layout, KV transfer between every eligible producer and consumer, and one active role controller. vLLM transfers KV through NIXL `kv_both`. SGLang transfers KV through the `mooncake` connector. TensorRT-LLM transfers KV through its C++ cache transceiver, over UCX or NIXL.

| Host | Responsibilities | Required software and inputs |
| --- | --- | --- |
| Management workstation | Generate private configuration, inspect hosts, package and install the Narwhal wheel, run trials, and retain evidence. | Git, Bash, Python 3.11+ with `venv`, Make, OpenSSH 8.5 or later, `uv`, and your site's access tools. The private `.env`, the release wheel, its dependency lock and their SHA-256 values, the known-hosts file, engine image, model paths and tree digest, fabric interface, run directory, ports, and credentials. |
| Router and observability host | Run the router and checks, and host Prometheus and Grafana. | Python 3.11+ with `venv` and access to a Python package index, coreutils, Git, Make, curl, Docker Engine, and Compose. The verified package, engine and attestation URLs, API credential, model settings, and SLOs. |
| Engine host | Run vLLM, SGLang or TensorRT-LLM and the attestation sidecar, and capture cache and transfer evidence. | Python 3.11+ with `venv` and access to a Python package index, coreutils, curl, accelerator driver, container runtime, and transfer devices. The accelerator allocation, TP shape, pinned image, checkpoint, launch policy, and fabric addresses and ports. |

### Concurrency

`narwhal-engine start-shared` starts engines that share a GPU one at a time and waits for each to answer before it starts the next. Measure each directed fabric edge while the fleet is idle.

Per-engine and per-host rules:

- Compare each engine allocation with the GPU inventory collected on that engine host.
- Install once per physical host.
- Capture each vLLM and TensorRT-LLM engine's live cache layout.

### Deployment record

Create a private deployment record before the first command. For every stage, retain the host or stable host alias, the wheel's SHA-256, starting state, executed commands, exit status, and generated artifacts.

On failure, record the state before changing it, retain the failed evidence, and remove only files and processes from that attempt. Replace private addresses, paths, and credentials with stable aliases before exporting evidence.

## Running the deployment

`narwhal deploy run` runs every stage of the [stage table](#rerunning-after-a-change) from host access to a ready router pair behind HAProxy with the monitoring stack running, and keeps each stage's evidence in its run directory. With `.env` loaded, check the inputs and hosts first:

```bash
narwhal deploy plan --run runs/deploy/first-deploy
narwhal deploy run --run runs/deploy/first-deploy
```

A failed stage prints its recovery section and evidence directory. Fix the cause and run `run` again: it resumes at that stage and skips every stage whose result still holds. `narwhal deploy status` shows each stage and checks the processes the run started, and `narwhal deploy down` stops them. [Running the stage graph](cli/Deploy.md#running-the-stage-graph) describes what each stage runs and the inputs it reads. The `monitoring` and `trial` stages still run by hand, as [Serve and measure](deploy/07-Serve-and-Measure.md) describes.

On a Kubernetes cluster, the same commands take `--target kubernetes` and run each stage through the Narwhal Helm chart; [Deploying on Kubernetes](deploy/Kubernetes.md) covers its inputs and stages.

## Deployment sequence

Each gate page below runs its stages by hand. Record each gate's result before the next gate.

<div class="grid cards" markdown>

-   [Discover the deployment](deploy/01-Discover.md)

    ---

    Freeze inputs and discover the deployment.

-   [Install Narwhal on the hosts](deploy/02-Install.md)

    ---

    Package and install the Narwhal wheel.

-   [Validate and start engines](deploy/03-Validate-Engines.md)

    ---

    Validate and start every engine.

-   [Qualify the transfer fabric](deploy/04-Qualify-Fabric.md)

    ---

    Prove the transfer fabric against the serving cache.

-   [Attest live engines](deploy/05-Attest.md)

    ---

    Attest the live engine processes.

-   [Profile engines and run preflight](deploy/06-Profile-and-Preflight.md)

    ---

    Profile once and run the live KV contract.

-   [Serve and measure](deploy/07-Serve-and-Measure.md)

    ---

    Start the service and validate capacity through the private path.

</div>

After the capacity trial passes, run [advisory mode](configuration/02-Serving-and-Role-Control.md#advisory-rollout) on representative traffic before you allow production role movement. Then archive the fleet configuration, the private inputs and the launch evidence in the private deployment record.

### Rerunning after a change

Each stage in the first table runs one section of a gate page. The stage runner keeps a [run record](telemetry/06-Deployment-Runs.md) of every stage: its input digests and the SHA-256 of each file it wrote. On the next run it skips a stage whose last attempt passed, whose digests still match and whose files are unchanged. Engine, sidecar and router processes and fabric routes are checked live first, and a stage whose process is gone runs again.

A change reruns the stages its row lists and no others. A change that touches several rows reruns all of them: an engine restart that changes the engine's attested binding digest reruns the engine process row and the binding digest row. A changed output file reruns its stage and every stage after it. When you repeat gates by hand, repeat the sections of the stages a change lists.

An engine whose container stopped or restarted also spends its launch directory, which starts one engine. `engine-prepare` writes a fresh directory, then `engine-check` and `engine-start` run from it, and the engine process row follows. A fresh directory of an unchanged launch record attests the same planned launch, so the binding digest row adds its stages only when the live captures differ.

<!-- Generated from src/narwhal/deployment/deploy_graph.py by tools/maintenance/stage_graph_doc.py. Edit the graph, then run the tool. -->

| Stage | Runs | After | Gate section |
| --- | --- | --- | --- |
| `access` | Once for each host in the inventory |  | [Running discovery and access checks](deploy/01-Discover.md#running-discovery-and-access-checks) |
| `discover` | Once per run, on the management workstation | `access` | [Running discovery and access checks](deploy/01-Discover.md#running-discovery-and-access-checks) |
| `package` | Once per run, on the management workstation | `discover` | [Building the deployment package](deploy/02-Install.md#building-the-deployment-package) |
| `install` | Once for each host in the inventory | `package` | [Installing engine 1 and the remaining hosts](deploy/02-Install.md#installing-engine-1-and-the-remaining-hosts) |
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
| `tunnel` | Once per run, on the management workstation | `monitoring` | [Tunnelling router, Prometheus, and Grafana to the workstation](deploy/07-Serve-and-Measure.md#tunnelling-router-prometheus-and-grafana-to-the-workstation) |
| `trial` | Once per run, on the management workstation | `tunnel` | [Running the initial capacity trial](deploy/07-Serve-and-Measure.md#running-the-initial-capacity-trial) |

| Change | Recorded as | Reruns |
| --- | --- | --- |
| SSH credentials, jump host or expected host keys | The password variable names, `NARWHAL_SSH_JUMP` and the SHA-256 of `NARWHAL_SSH_KNOWN_HOSTS` | `access` |
| SSH destination or roles of a host | Every host's SSH destination and roles | `access`, `package` and every stage after it |
| Hardware, model, image, checkpoint, launch policy, route or transport | The discovery fields of `.env`, the live image ID and the checkpoint tree digest | `discover` and every stage after it |
| Approved release | `NARWHAL_WHEEL_SHA256`, which each role environment records as `NARWHAL_DEPLOYMENT_REVISION`, and the SHA-256 of the lock, `NARWHAL_REQUIREMENTS_SHA256` | `package` and every stage after it |
| SLO targets or router overhead | `slo.ttft_s`, `slo.tpot_s` and `slo.router_overhead_s` | `fleet-limits`, `preflight`, `router-start`, `trial` |
| First-token deadline margin | The margin above the calibrated first-token candidate | `fleet-limits`, `preflight`, `router-start`, `trial` |
| Profiling sweep or warmup, or a trial rate or request count outside the profiled range | The prefill and decode sweep lengths, the decode concurrency and the warmup requests | `profile`, `preflight`, `router-start`, `trial` |
| First-token calibration inputs | The input tokens, samples and observation timeout | `calibrate`, `fleet-limits`, `preflight`, `router-start`, `trial` |
| Fabric budget workload | The prompt tokens, handoffs per second, burst, transfer budget and headroom | `transfer-measure` on SGLang when the prompt tokens change, `fabric-budget`, `fabric-edge` |
| Fabric test settings | The test port, and each engine's RDMA GID index and RoCE mode | `fabric-edge` |
| Router pair settings | Both routers' origins, the load balancer's origin, the lease path and the HAProxy image | `lease-check` on fleets with a standby router, `router-start`, `load-balancer` on fleets with a standby router, `monitoring`, `trial` |
| Monitoring settings | The Prometheus and Grafana listen addresses | `monitoring` |
| Trial workload, rate, request count or thresholds, within the profiled range | The trial workload, offered rate, request count and thresholds | `trial` from a drained router |
| Launch directory whose engine has stopped | The launch directory and the SHA-256 of its `launch.json`, checked live on every run | `engine-check`, `engine-start` for that engine |
| Engine process restarted | The container ID and start time, or the PID, boot ID and start ticks, checked live on every run | `engine-probe`, `cache-capture` on TensorRT-LLM or vLLM, `transfer-measure` on SGLang, `attest-capture`, `sidecar-start`, `sidecar-verify` for that engine; `finalize-fleet`, `preflight`, `router-start` to readmit the engine to a router that still runs |
| Engine cache geometry | The SHA-256 of `cache-layout.json` | `cache-groups`; `fabric-budget` for the engine's cache group; `fabric-edge` for every edge leaving a host of the engine's group |
| Measured KV handoff size of an engine | The SHA-256 of the engine's `transfer.json` handoff measurement | `cache-groups`; `fabric-budget` for the engine's cache group; `fabric-edge` for every edge leaving a host of the engine's group |
| Fabric route of a directed edge | The link fingerprint: roles, addresses, interfaces, both live routes, transport, tool version and test parameters, checked live on every run | `fabric-edge` for that edge; `calibrate`, `fleet-limits`, `preflight`, `router-start`, `trial` |
| Attested binding digest of an engine | The `launch_digest`, or the `attestation_digest` when the attestation has no launch evidence | `profile`, `calibrate`, `fleet-limits`, `preflight`, `router-start`, `trial` |
| Engine contract fields of an engine | The `EngineContract` fields of its attestation | `attest-capture` for every engine; `finalize-fleet`, `profile`, `calibrate`, `fleet-limits`, `preflight`, `router-start` |
| Attestation sidecar restarted | The sidecar's recorded process identity, checked live on every run | `sidecar-start`, `sidecar-verify` for that engine; `finalize-fleet` |
| Router restarted | The router's recorded process identity, checked live on every run | `router-start` for that router host |
| Load balancer restarted | The HAProxy container ID and start time, checked live on every run | `load-balancer` on fleets with a standby router |
| Monitoring container stopped or replaced | The container ID of each monitoring service, checked live on every run | `monitoring` |

<!-- End of the generated stage tables. -->

## Evidence and recovery index

Record these inputs and artifacts in the private deployment record:

| Stage | Inputs | Evidence to retain |
| --- | --- | --- |
| Discovery and access | `.env`, installed image and model, the checkpoint tree digest, GPU inspection utilities, launch policy, SSH route and key, the known-hosts file | `.env`, the files discovery writes under `config/`, `runs/discovery/<run>/`, `runs/access-<id>/` |
| Package and install | The wheel and its SHA-256, role mapping, per-engine allocation, host prerequisites | `runs/deployment-env/<run>/`, the remote `~/Narwhal-deploy/<id>/` directory |
| Host and engine validation | PCI accelerator identity, visible devices, allocation, TP size, image identity, checkpoint tree digest, paths, and ports | Engine role env, each engine's `ENGINE_RUN` directory, the discovery checkpoint manifests |
| Fabric | Idle engines, fabric routes, test port, transport and RDMA GID entries, representative cache layouts or handoffs, workload budget | The `narwhal deploy fabric` output: `fabric.json`, each group's `budget.json` and its sources, and the route, link, sample and evidence files of every directed sample |
| Attestation | Checked live process, model dimensions, and sidecar endpoint, with the vLLM protocol version, cache layout, transfer mode, and handshake policy | In each `ENGINE_RUN` directory, the `attestation/` attempt directories [`capture`](deploy/05-Attest.md#capturing-the-attestation) writes, `published-attestation.json` and the `attestation-check-*` directory from `verify`; `runs/deployment/fleet.json`, `runs/deployment/fleet.before-attestation-<id>.json` |
| Profiling | Idle engines, fixed runtime, generated sequence limits, prompt lengths, and concurrency | `runs/deployment/fleet.json`, `runs/deployment/profiling-limits.json`, the profile store and its `.samples.json` file |
| Preflight | Current processes, profiles, first-token calibration, fleet contract, and SLOs | Router environment, `runs/deployment/fleet.json`, `runs/deployment/first-token-calibration.json`, the `narwhal-check` output |
| Router | Bind address, model, engine count, and role split | Router env, fleet config, endpoint captures |
| Capacity trial | Workstation load client, router observability stack, SSH path, fixed runtime and cache policy, workload, and thresholds | Tunnel logs in `runs/access-<id>/`, `runs/load-trial-<id>/` |

Recover a failed stage with the steps on its page:

- Discovery and access: [Access failure handling](deploy/01-Discover.md#access-failure-handling).
- Package and install: [Recovering a failed installation](deploy/02-Install.md#recovering-a-failed-installation).
- Fabric: [Troubleshooting an edge below its budget](deploy/04-Qualify-Fabric.md#troubleshooting-an-edge-below-its-budget).
- Attestation: when `capture` fails, fix the cause it names and run `capture` again, as in [Capturing the attestation](deploy/05-Attest.md#capturing-the-attestation). When `verify` or `finalize-fleet` fails, fix the failing engine, restart its sidecar against the checked process, and run `finalize-fleet` again.
- Profiling: [Reusing or repeating profiles](deploy/06-Profile-and-Preflight.md#reusing-or-repeating-profiles).
- Preflight: follow the reported engine, transfer leg or budget into [Troubleshooting a fleet](Troubleshoot.md).
- Router and capacity trial: [Troubleshooting the tunnel](deploy/07-Serve-and-Measure.md#troubleshooting-the-tunnel).

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

