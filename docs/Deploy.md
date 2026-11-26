---
description: Deploy Narwhal on a multi-node vLLM, SGLang or TensorRT-LLM fleet, from host discovery to live serving.
---

# Deploying a fleet

## Requirements

Deploy one model per fleet. Its engines need a compatible KV layout, KV transfer between every eligible producer and consumer, and one active role controller. vLLM transfers KV through NIXL `kv_both`. SGLang transfers KV through the `mooncake` connector. TensorRT-LLM transfers KV through its C++ cache transceiver, over UCX or NIXL.

| Host | Responsibilities | Required software and inputs |
| --- | --- | --- |
| Management workstation | Generate private configuration, inspect hosts, package and install the approved revision, run trials, and retain evidence. | Git, Bash, Python 3.11+ with `venv`, Make, OpenSSH, your site's access tools, and `sshpass` for password authentication. The private `.env`, approved revision, engine image, model paths, fabric interface, run directory, ports, and credentials. |
| Router and observability host | Run the router and checks, and host Prometheus and Grafana. | Python 3.11+ with `venv`, Git, Make, curl, Docker Engine, and Compose. The verified bundle and revision, engine and attestation URLs, API credential, model settings, and SLOs. |
| Engine host | Run vLLM, SGLang or TensorRT-LLM and the attestation sidecar, and capture cache and transfer evidence. | Python 3.11+ with `venv`, Git, Make, curl, accelerator driver, container runtime, and transfer devices. The accelerator allocation, TP shape, pinned image, checkpoint, launch policy, and fabric addresses and ports. |

### Concurrency

`narwhal-engine start-shared` starts engines that share a GPU one at a time and waits for each to answer before it starts the next. Measure each directed fabric edge while the fleet is idle.

Per-engine and per-host rules:

- Compare each engine allocation with the GPU inventory collected on that engine host.
- Install once per physical host.
- Capture each vLLM and TensorRT-LLM engine's live cache layout.

### Deployment record

Create a private deployment record before the first command. For every stage, retain the host or stable host alias, source revision, starting state, executed commands, exit status, and generated artifacts.

On failure, record the state before changing it, retain the failed evidence, and remove only files and processes from that attempt. Replace private addresses, paths, and credentials with stable aliases before exporting evidence.

## Deployment sequence

Record each gate's result before the next gate.

<div class="grid cards" markdown>

-   [Discover the deployment](deploy/01-Discover.md)

    ---

    Freeze inputs and discover the deployment.

-   [Install the approved revision](deploy/02-Install.md)

    ---

    Package and install the approved revision.

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

<!-- Generated from src/narwhal/deployment/deploy_graph.py by tools/maintenance/stage_graph_doc.py. Edit the graph, then run the tool. -->

| Stage | Runs | After | Gate section |
| --- | --- | --- | --- |
| `access` | Once for each host in the inventory |  | [Running discovery and access checks](deploy/01-Discover.md#running-discovery-and-access-checks) |
| `discover` | Once per run, on the management workstation | `access` | [Running discovery and access checks](deploy/01-Discover.md#running-discovery-and-access-checks) |
| `package` | Once per run, on the management workstation | `discover` | [Building an immutable deployment package](deploy/02-Install.md#building-an-immutable-deployment-package) |
| `install` | Once for each host in the inventory | `package` | [Installing engine 1 and the remaining hosts](deploy/02-Install.md#installing-engine-1-and-the-remaining-hosts) |
| `engine-inspect` | Once for each engine role, on its engine host | `install` | [Inspecting every engine host](deploy/03-Validate-Engines.md#inspecting-every-engine-host) |
| `engine-prepare` | Once for each engine role, on its engine host | `engine-inspect` | [Preparing, checking, and starting each engine](deploy/03-Validate-Engines.md#preparing-checking-and-starting-each-engine) |
| `engine-check` | Once for each engine role, on its engine host | `engine-prepare` | [Preparing, checking, and starting each engine](deploy/03-Validate-Engines.md#preparing-checking-and-starting-each-engine) |
| `engine-start` | Once for each engine role, on its engine host | `engine-check` | [Preparing, checking, and starting each engine](deploy/03-Validate-Engines.md#preparing-checking-and-starting-each-engine) |
| `engine-probe` | Once for each engine role, on its engine host | `engine-start` | [Proving the live HTTP process](deploy/03-Validate-Engines.md#proving-the-live-http-process) |
| `cache-capture` | Once for each engine role, on its engine host, on TensorRT-LLM or vLLM | `engine-probe` | [Capturing the live cache layout](deploy/03-Validate-Engines.md#capturing-the-live-cache-layout) |
| `cache-groups` | Once per run, on the management workstation | `discover`, `cache-capture` | [Deriving cache-equivalence groups](deploy/03-Validate-Engines.md#deriving-cache-equivalence-groups) |
| `fabric-budget` | Once for each cache-equivalence group | `cache-groups` | [Building the source budget](deploy/04-Qualify-Fabric.md#building-the-source-budget) |
| `fabric-edge` | Once for each directed edge between engine hosts | `fabric-budget` | [Binding each sample to a directed route](deploy/04-Qualify-Fabric.md#binding-each-sample-to-a-directed-route) |
| `fleet-inventory` | Once for each router host | `install` | [Checking the router inventory](deploy/05-Attest.md#checking-the-router-inventory) |
| `attest-capture` | Once for each engine role, on its engine host | `fabric-edge`, `fleet-inventory` | [Capturing the attestation inputs](deploy/05-Attest.md#capturing-the-attestation-inputs) |
| `sidecar-start` | Once for each engine role, on its engine host | `attest-capture` | [Starting the sidecar](deploy/05-Attest.md#starting-the-sidecar) |
| `sidecar-verify` | Once for each engine role, on its engine host | `sidecar-start` | [Verifying the sidecar against the engine](deploy/05-Attest.md#verifying-the-sidecar-against-the-engine) |
| `finalize-fleet` | Once for each router host | `sidecar-verify` | [Finalizing the fleet contract](deploy/05-Attest.md#finalizing-the-fleet-contract) |
| `profile` | Once for each router host | `finalize-fleet` | [Profiling idle engines](deploy/06-Profile-and-Preflight.md#profiling-idle-engines) |
| `calibrate` | Once for each router host | `profile` | [Calibrating the first-token deadline](deploy/06-Profile-and-Preflight.md#calibrating-the-first-token-deadline) |
| `fleet-limits` | Once per run, on the management workstation | `calibrate` | [Reusing or repeating the calibration](deploy/06-Profile-and-Preflight.md#reusing-or-repeating-the-calibration) |
| `preflight` | Once for each router host | `fleet-limits` | [Running preflight](deploy/06-Profile-and-Preflight.md#running-preflight) |
| `router-start` | Once for each router host | `preflight` | [Starting and locally verifying the router](deploy/07-Serve-and-Measure.md#starting-and-locally-verifying-the-router) |
| `monitoring` | Once for each router host | `router-start` | [Starting the monitoring stack on the router](deploy/07-Serve-and-Measure.md#starting-the-monitoring-stack-on-the-router) |
| `tunnel` | Once per run, on the management workstation | `monitoring` | [Tunnelling router, Prometheus, and Grafana to the workstation](deploy/07-Serve-and-Measure.md#tunnelling-router-prometheus-and-grafana-to-the-workstation) |
| `trial` | Once per run, on the management workstation | `tunnel` | [Running the initial capacity trial](deploy/07-Serve-and-Measure.md#running-the-initial-capacity-trial) |

| Change | Recorded as | Reruns |
| --- | --- | --- |
| SSH credentials or expected host keys | The password variable names and the SHA-256 of the expected host-key file | `access` |
| SSH destination of a host | Every host's SSH destination | `access`, `package` and every stage after it |
| Hardware, model, image, checkpoint, launch policy, route or transport | The discovery fields of `.env`, the live image ID and the checkpoint tree digest | `discover` and every stage after it |
| Approved revision | `NARWHAL_DEPLOYMENT_REVISION` and the SHA-256 of the Narwhal package hosts install | `package` and every stage after it |
| SLO targets | `slo.ttft_s` and `slo.tpot_s` | `fleet-limits`, `preflight`, `router-start`, `trial` |
| First-token deadline margin | The margin above the calibrated first-token candidate | `fleet-limits`, `preflight`, `router-start`, `trial` |
| Profiling sweep or warmup, or a trial rate or request count outside the profiled range | The prefill and decode sweep lengths, the decode concurrency and the warmup requests | `profile`, `preflight`, `router-start`, `trial` |
| First-token calibration inputs | The input tokens, samples and observation timeout | `calibrate`, `fleet-limits`, `preflight`, `router-start`, `trial` |
| Fabric budget workload | The prompt tokens, handoffs per second, burst, transfer budget and headroom | `fabric-budget`, `fabric-edge` |
| Fabric test settings | The test port, test durations, and each host's RDMA device, port, GID index and RoCE mode | `fabric-edge` |
| Router settings | The bind address, port, lease path, standby options and load balancer | `router-start`, `monitoring`, `trial` |
| Monitoring origins | The router and load balancer origins and the listen addresses | `monitoring` |
| Trial workload, rate, request count or thresholds, within the profiled range | The trial workload, offered rate, request count and thresholds | `trial` from a drained router |
| Engine process restarted | The container ID and start time, or the PID, boot ID and start ticks, checked live on every run | `engine-probe`, `cache-capture` on TensorRT-LLM or vLLM, `attest-capture`, `sidecar-start`, `sidecar-verify` for that engine; `finalize-fleet`, `preflight` |
| Engine cache geometry | The SHA-256 of `cache-layout.json` | `cache-groups`; `fabric-budget` for the engine's cache group; `fabric-edge` for every edge leaving a host of the engine's group |
| Fabric route of a directed edge | The link fingerprint: roles, addresses, interfaces, both live routes, transport, tool version and test parameters, checked live on every run | `fabric-edge` for that edge; `calibrate`, `fleet-limits`, `preflight`, `router-start`, `trial` |
| Attested binding digest of an engine | The `launch_digest`, or the `attestation_digest` when the attestation has no launch evidence | `profile`, `calibrate`, `fleet-limits`, `preflight`, `router-start`, `trial` |
| Engine contract fields of an engine | The `EngineContract` fields of its attestation | `attest-capture` for every engine; `finalize-fleet`, `profile`, `calibrate`, `fleet-limits`, `preflight`, `router-start` |
| Attestation sidecar restarted | The sidecar's recorded process identity, checked live on every run | `sidecar-start`, `sidecar-verify` for that engine; `finalize-fleet` |
| Router restarted | The router's recorded process identity, checked live on every run | `router-start` for that router host |

<!-- End of the generated stage tables. -->

## Evidence and recovery index

Record these inputs and artifacts in the private deployment record:

| Stage | Inputs | Evidence to retain |
| --- | --- | --- |
| Discovery and access | `.env`, installed image and model, GPU inspection utilities, launch policy, SSH route and key | `.env`, the files discovery writes under `config/`, `runs/discovery/<run>/`, `runs/access-<id>/` |
| Package and install | Approved commit, role mapping, per-engine allocation, host prerequisites | `runs/deployment-env/<run>/`, the remote `~/Narwhal-deploy/<id>/` directory |
| Host and engine validation | PCI accelerator identity, visible devices, allocation, TP size, image identity, checkpoint tree digest, paths, and ports | Engine role env, each engine's `ENGINE_RUN` directory, the discovery checkpoint manifests |
| Fabric | Peer addresses, transport, representative process, live cache layout, workload budget | `budget.json`, `cache-layout.json`, and the `<edge>.*` files of every directed edge |
| Attestation | Checked live process, model dimensions, and sidecar endpoint, with the vLLM protocol version, cache layout, transfer mode, and handshake policy | `engine-attestation.json` and `startup.log` in each `ENGINE_RUN` directory, `runs/deployment/fleet.json`, `runs/fleet.before-attestation-<id>.json` |
| Profiling | Idle engines, fixed runtime, generated sequence limits, prompt lengths, and concurrency | `runs/deployment/fleet.json`, `runs/deployment/profiling-limits.json`, the profile store and its `.samples.json` file |
| Preflight | Current processes, profiles, first-token calibration, fleet contract, and SLOs | Router environment, `runs/deployment/fleet.json`, `runs/deployment/first-token-calibration.json`, the `narwhal-check` output |
| Router | Bind address, model, engine count, and role split | Router env, fleet config, endpoint captures |
| Capacity trial | Workstation load client, router observability stack, SSH path, fixed runtime and cache policy, workload, and thresholds | Tunnel logs in `runs/access-<id>/`, `runs/load-trial-<id>/` |

Recover a failed stage with the steps on its page:

- Discovery and access: [Access failure handling](deploy/01-Discover.md#access-failure-handling).
- Package and install: [Recovering a failed installation](deploy/02-Install.md#recovering-a-failed-installation).
- Fabric: [Troubleshooting an edge below its budget](deploy/04-Qualify-Fabric.md#troubleshooting-an-edge-below-its-budget).
- Attestation: fix the failing engine, restart its sidecar against the checked process, and run `finalize-fleet` again.
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

