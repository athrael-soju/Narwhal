---
description: Deploy Narwhal on a multi-node vLLM fleet with NIXL KV transfer, from host discovery to live serving.
---

# Deploying a fleet

## Requirements

Deploy one model per fleet. Its vLLM engines need a compatible KV layout, NIXL `kv_both` transfer between every eligible producer and consumer, and one active role controller.

| Host | Responsibilities | Required software and inputs |
| --- | --- | --- |
| Management workstation | Generate private configuration, inspect hosts, package and install the approved revision, run trials, and retain evidence. | Git, Bash, Python 3.11+, OpenSSH, supplied access tooling, and `sshpass` for password authentication. The private `.env`, approved revision, engine image, model paths, fabric interface, run directory, ports, and credentials. |
| Router and observability host | Run the router and checks, and host Prometheus and Grafana. | Python 3.11+ with `venv`, Git, Make, curl, Docker Engine, and Compose. The verified bundle and revision, engine and attestation URLs, API credential, model settings, and SLOs. |
| Engine host | Run vLLM and the attestation sidecar, and capture cache and transfer evidence. | Python 3.11+ with `venv`, Git, Make, curl, accelerator driver, container runtime, and transfer devices. The accelerator allocation, TP shape, pinned image, checkpoint, launch policy, and fabric addresses and ports. |

### Concurrency

Inspect engine hosts, start engines with disjoint GPU allocations, and attest running engines after the fabric check in parallel. Start engines that share GPUs one at a time. Measure one directed fabric edge at a time with idle engines.

Per-engine and per-host rules:

- Compare each engine allocation with the GPU inventory collected on that engine host.
- Install once per physical host.
- Capture each engine's live cache layout.

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
### Rerunning after a change

| Change | Repeat |
| --- | --- |
| Offered rate or request count within the profiled range | Drain the router and run the next trial point with the existing profiles, fabric samples, and preflight. |
| SLO or first-token deadline | Run `narwhal-check` with the revised limits against saved profiles and live handoffs, then load the edited fleet. |
| Router restart with unchanged fleet configuration | Restart the router. |
| Engine restart with unchanged attested [`launch_digest`](configuration/01-Fleet-Schema.md#attestation) | Capture the live cache layout and attestation, then run full preflight against saved profiles and the [first-token calibration](deploy/06-Profile-and-Preflight.md#calibrating-the-first-token-deadline). |
| Engine restart with changed attestation or only an attestation digest | Capture the live cache layout and attestation, profile the new process generation, recalibrate the first-token deadline, then run full preflight. |
| Changed cache geometry | Recalculate the fabric budget from `cache-layout.json`. |
| Changed fabric route, host assignment, or transport | Measure affected directed links against the source budget, recalibrate the first-token deadline, and exercise the live KV paths in preflight. |

## Evidence and recovery index

Record these inputs and artifacts in the private deployment record:

| Stage                      | Inputs                                                                                                                    | Evidence to retain                                                                                                               |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| Discovery and access       | `.env`, installed image and model, GPU inspection utilities, launch policy, SSH route and key                             | `.env`, generated JSON, host-key database, `runs/discovery/<run>/`, `runs/access-<id>/`                                          |
| Package and install        | Approved commit, role mapping, per-engine allocation, host prerequisites                                                  | `runs/deployment-env/<run>/`, remote `~/Narwhal-deploy/<id>/`, role files, fleet config, install marker                          |
| Host and engine validation | PCI accelerator identity, visible devices, allocation, TP size, image identity, checkpoint tree digest, paths, and ports  | Engine role env, launch record, discovery checkpoint manifests, `model_tree_sha256`, `ENGINE_RUN`, image-check and HTTP captures |
| Fabric                     | Peer addresses, transport, representative process, live cache layout, workload budget                                     | Cache layout, budget, link fingerprints, route files, directed samples, comparisons, edge matrix                                 |
| Attestation                | Checked live process, protocol version, model dimensions, cache layout, transfer mode, handshake policy, sidecar endpoint | `engine-attestation.json`, all `ENGINE_RUN` captures, router fleet before and after attestation                                  |
| Profiling                  | Idle engines, fixed runtime, generated sequence limits, prompt lengths, and concurrency                                   | Fleet config, profiling limits, profile and sample files                                                                         |
| Preflight                  | Current processes, profiles, first-token calibration, fleet contract, and SLOs                                            | Router environment, fleet config, calibration artifact, private check output                                                     |
| Router                     | Bind address, model, engine count, and role split                                                                         | Router env, fleet config, endpoint captures                                                                                      |
| Capacity trial             | Workstation load client, router observability stack, SSH path, fixed runtime and cache policy, workload, and thresholds   | Tunnel logs, Compose discovery, `runs/load-trial-<id>/`, manifests, request records, summaries, state and network snapshots      |

Correct a failed stage from its evidence:

- Discovery and access: fix the named environment field, missing host utility, image inspection issue, credential, route, or independently verified changed host key.
- Package and install:
    1. Create a new prepared run from corrected input.
    2. Resume dependency installation after fixing the host cause.
- Host and engine validation:
    1. Restore device exposure or artifacts, free the planned ports, or repair the differing checkpoint file.
    2. Repeat discovery.
- Fabric:
    1. Identify the root cause in route, binding, listener, firewall, HCA/GID, MTU, retransmissions or RDMA counters, CPU saturation, or concurrent traffic.
    2. Re-sample.
- Attestation:
    1. Inspect the bound capture and source.
    2. Restart only the affected process or sidecar.
    3. Rerun finalization.
- Profiling:
    1. Correct the failing engine or sweep.
    2. Write new samples to a fresh profile path.
- Preflight: follow the reported engine, transfer leg, or budget into the matching troubleshooting path.
- Router: inspect the listener's process, the address family, the router error, and the upstream engine error.
- Capacity trial:
    1. Resolve local port conflicts, remote listeners, scrape failures, client saturation, scheduler lag, and reconciliation gaps.
    2. Attribute any remaining SLO miss to serving capacity.

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

