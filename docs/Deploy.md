---
description: Deploy Narwhal on a multi-node vLLM fleet with NIXL KV transfer, from host discovery to live serving.
---

# Deploying a fleet

## Operating model

Fleet requirements:

- one model on vLLM engines that share a compatible KV layout
- KV transfer through NIXL with effective `kv_both` behaviour
- transfer from every eligible KV producer to every eligible consumer
- one active role controller per fleet

A deployment uses three kinds of machine. Each one needs its software and inputs in place before the first gate.

The **management workstation** holds credentials, generates private configuration, inspects remote hosts, packages the approved source revision, drives installation, opens role shells, tunnels trial traffic and retains evidence. It runs Git, Bash, Python 3.11 or newer, OpenSSH, the supplied access tooling, and `sshpass` for password-based SSH. Its inputs are the private `.env`, the approved source revision, the engine image, the model name and paths, the fabric interface, the run directory, the service ports, and the management destinations and credentials.

The **router and observability host** holds the fleet configuration and profiles, runs the Narwhal checks and the router, and hosts Prometheus and Grafana. It runs Python 3.11 or newer with `venv`, Git, Make, curl, Docker Engine and the Compose plugin. Its inputs are the verified source bundle and revision, the engine and attestation URLs, the API credential, and the model and SLO settings.

The **engine hosts** expose accelerators and transfer devices, run vLLM and the attestation sidecars, and provide cache and transfer evidence. Each engine host runs Python 3.11 or newer with `venv`, Git, Make and curl, plus the configured accelerator driver, container runtime and transfer devices. Its inputs are the accelerator allocation, TP shape, pinned image, checkpoint, launch policy, and fabric addresses and ports.

### Concurrency rules

Engine host inspection, engine starts with disjoint GPU allocations, and attestation of running engines after Gate D run concurrently. Engines that share GPUs start one at a time. Fabric measurement runs one directed edge at a time, with the engines idle.

Per-engine and per-host rules:

- Compare each engine allocation with the GPU inventory collected on that engine host.
- Install once per physical host.
- Capture each engine's live cache layout.

### Deployment record

Create a private deployment record before the first command.

For every gate, retain:

- host or stable host alias
- full source revision
- starting state
- commands executed
- exit status
- generated artifacts and paths

When a gate fails:

1. Record the failed state before changing it.
2. Retain the failed attempt's evidence.
3. Remove only the files and processes the failed attempt created.

Replace private addresses, paths, and credentials with stable aliases before exporting evidence from the private environment.

## Deployment sequence

Record each gate's result before the next gate.

<div class="grid cards" markdown>

-   [Gate A](deploy/01-Discover.md)

    ---

    Freeze inputs and discover the deployment.

-   [Gate B](deploy/02-Install.md)

    ---

    Package and install the approved revision.

-   [Gate C](deploy/03-Validate-Engines.md)

    ---

    Validate and start every engine.

-   [Gate D](deploy/04-Qualify-Fabric.md)

    ---

    Prove the transfer fabric against the serving cache.

-   [Gate E](deploy/05-Attest.md)

    ---

    Attest the live engine processes.

-   [Gate F](deploy/06-Profile-and-Preflight.md)

    ---

    Profile once and run the live KV contract.

-   [Gate G](deploy/07-Serve-and-Measure.md)

    ---

    Start the service and validate capacity through the private path.

</div>

### Repeating work after a change

A change to a running deployment repeats the part of the sequence it affects.

For a new offered rate or request count within the profiled workload range:

1. Drain the router.
2. Run the next trial point on the existing engine profiles, fabric samples, and full preflight.

For a new SLO or first-token deadline:

1. Run `narwhal-check` with the revised limits against the saved profiles and live handoffs.
2. Load the edited fleet in the router.

For a router restart with the same fleet configuration, restart the router.

For an engine restart with an unchanged attested [`launch_digest`](configuration/01-Fleet-Schema.md#33-attestation):

1. Capture its live cache layout and attestation.
2. Run full preflight against the saved profiles and [first-token calibration](deploy/06-Profile-and-Preflight.md#calibrating-the-first-token-deadline).

For an engine restart with a changed attested `launch_digest`, or a sidecar that reports an attestation digest only:

1. Capture its live cache layout and attestation.
2. Profile the new process generation.
3. [Recalibrate the first-token deadline](deploy/06-Profile-and-Preflight.md#calibrating-the-first-token-deadline).
4. Run full preflight.

When an engine restart changes the cache geometry, recalculate the engine's fabric budget from the new `cache-layout.json`.

For a change to the fabric route, host assignment, or transport:

1. Measure the affected directed links against the source budget.
2. [Recalibrate the first-token deadline](deploy/06-Profile-and-Preflight.md#calibrating-the-first-token-deadline).
3. Exercise the live KV paths in preflight.

## Evidence and recovery index

Each gate has defining inputs and evidence to retain in the private deployment record:

| Gate                       | Inputs that define the gate                                                                                               | Evidence to retain                                                                                                               |
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

Correct a failed gate by its stage:

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

### External sources

These upstream sources cover the environment template, model staging, ROCm containers, the pinned vLLM release and the fabric test tools.

<div class="grid cards" markdown>

-   [Environment template](https://github.com/athrael-soju/Narwhal/blob/main/.env.example)

    ---

    Narwhal `.env.example` on GitHub.

-   [Snapshot download](https://huggingface.co/docs/huggingface_hub/guides/download)

    ---

    Hugging Face Hub guide.

-   [Model cards](https://huggingface.co/docs/hub/model-cards)

    ---

    Hugging Face Hub documentation.

-   [Container device guidance](https://rocm.docs.amd.com/projects/install-on-linux/en/latest/how-to/docker.html)

    ---

    ROCm installation documentation.

-   [NIXL connector usage](https://docs.vllm.ai/en/v0.30.0/features/nixl_connector_usage/)

    ---

    vLLM v0.30.0 documentation.

-   [Mamba layout resolver](https://github.com/vllm-project/vllm/blob/v0.30.0/vllm/model_executor/layers/mamba/mamba_utils.py)

    ---

    vLLM v0.30.0 source.

-   [KV cache interface](https://github.com/vllm-project/vllm/blob/v0.30.0/vllm/v1/kv_cache_interface.py)

    ---

    vLLM v0.30.0 source.

-   [KV cache grouping](https://github.com/vllm-project/vllm/blob/v0.30.0/vllm/v1/core/kv_cache_utils.py)

    ---

    vLLM v0.30.0 source.

-   [NIXL metadata and compatibility hash](https://github.com/vllm-project/vllm/blob/v0.30.0/vllm/distributed/kv_transfer/kv_connector/v1/nixl/metadata.py)

    ---

    vLLM v0.30.0 source.

-   [Model architecture conversion](https://github.com/vllm-project/vllm/blob/v0.30.0/vllm/transformers_utils/model_arch_config_convertor.py)

    ---

    vLLM v0.30.0 source.

-   [KV cache layout enum](https://github.com/vllm-project/vllm/blob/v0.30.0/vllm/v1/kv_cache_layout.py)

    ---

    vLLM v0.30.0 source.

-   [NIXL worker](https://github.com/vllm-project/vllm/blob/v0.30.0/vllm/distributed/kv_transfer/kv_connector/v1/nixl/base_worker.py)

    ---

    vLLM v0.30.0 source.

-   [Attention backend utilities](https://github.com/vllm-project/vllm/blob/v0.30.0/vllm/v1/attention/backends/utils.py)

    ---

    vLLM v0.30.0 source.

-   [NIXL connector aliases](https://github.com/vllm-project/vllm/blob/v0.30.0/vllm/distributed/kv_transfer/kv_connector/v1/nixl/connector.py)

    ---

    vLLM v0.30.0 source.

-   [iperf3 command reference](https://software.es.net/iperf/invoking.html)

    ---

    iperf3 documentation.

-   [perftest repository](https://github.com/linux-rdma/perftest)

    ---

    perftest source on GitHub.

</div>
