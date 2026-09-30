---
description: Deploy Narwhal on a multi-node vLLM fleet with NIXL KV transfer, from host discovery to live serving.
---

# Deploy a fleet

## Operating model

Fleet requirements:

- one model on vLLM engines that share a compatible KV layout
- KV transfer through NIXL with effective `kv_both` behaviour
- transfer from every eligible KV producer to every eligible consumer
- one active role controller per fleet

| Role                          | Responsibilities                                                                                                                                                                           | Inputs that must already exist                                                                                                                                         |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Management workstation        | Hold credentials, generate private configuration, inspect remote hosts, package the approved source revision, drive installation, open role shells, tunnel trial traffic, retain evidence. | Private `.env`, approved source revision, engine image, model name and paths, fabric interface, run directory, service ports, management destinations and credentials. |
| Router and observability host | Hold the fleet configuration and profiles, run Narwhal checks and router, host Prometheus and Grafana.                                                                                     | Verified source bundle and revision, engine and attestation URLs, API credential, model and SLO settings, Docker Engine and Compose plugin.                            |
| Engine hosts                  | Expose accelerators and transfer devices, run vLLM and attestation sidecars, provide cache and transfer evidence.                                                                          | Accelerator allocation, TP shape, pinned image, checkpoint, launch policy, fabric addresses and ports.                                                                 |

| Machine                                | Required software                                                                                            |
| -------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| Management workstation                 | Git, Bash, Python 3.11 or newer, OpenSSH, the supplied access tooling, and `sshpass` for password-based SSH. |
| Remote host that runs Narwhal commands | Python 3.11 or newer with `venv`, Git, Make, and curl.                                                       |
| Engine host                            | The remote host software plus the configured accelerator driver, container runtime, and transfer devices.    |

### Concurrency rules

Per-engine and per-host rules:

- Compare each engine allocation with the GPU inventory collected on that engine host.
- Install once per physical host.
- Capture each engine's live cache layout.

| Work                                        | Scheduling                                     |
| ------------------------------------------- | ---------------------------------------------- |
| Engine host inspection                      | Concurrent                                     |
| Engine start with disjoint GPU allocations  | Concurrent                                     |
| Engine start with shared GPUs               | One engine at a time                           |
| Fabric measurement                          | One directed edge at a time, with engines idle |
| Attestation of running engines after Gate D | Concurrent                                     |

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

Work to repeat by change:

| Change                                                           | Work to repeat                                                                                                                                                                                                                             |
| ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Offered rate or request count within the profiled workload range | 1. Drain the router.<br>2. Run the next trial point on the existing engine profiles, fabric samples, and full preflight.                                                                                                                   |
| SLO or first-token deadline                                      | 1. Run `narwhal-check` with the revised limits against the saved profiles and live handoffs.<br>2. Load the edited fleet in the router.                                                                                                    |
| Router restart with the same fleet configuration                 | Restart the router.                                                                                                                                                                                                                        |
| Engine restart whose sidecar attests the `launch_digest` recorded in the saved profiles and calibration | 1. Capture its live cache layout and attestation.<br>2. Run full preflight against the saved profiles and [first-token calibration](deploy/06-Profile-and-Preflight.md#calibrate-the-first-token-deadline). |
| Engine restart whose sidecar attests a `launch_digest` different from the saved profiles and calibration, or reports `attestation_digest` as its only digest | 1. Capture its live cache layout and attestation.<br>2. Profile the new process generation.<br>3. [Recalibrate the first-token deadline](deploy/06-Profile-and-Preflight.md#calibrate-the-first-token-deadline).<br>4. Run full preflight. |
| Cache geometry change after an engine restart                    | Recalculate the engine's fabric budget from the new `cache-layout.json`.                                                                                                                                                                   |
| Fabric route, host assignment, or transport                      | 1. Measure the affected directed links against the source budget.<br>2. [Recalibrate the first-token deadline](deploy/06-Profile-and-Preflight.md#calibrate-the-first-token-deadline).<br>3. Exercise the live KV paths in preflight.      |

## Evidence and recovery index

Inputs and evidence by gate:

| Gate                       | Inputs that define the gate                                                                                                | Evidence to retain                                                                                                                |
| -------------------------- | -------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| Discovery and access       | `.env`, installed image and model, GPU inspection utilities, launch policy, SSH route and key.                             | `.env`, generated JSON, host-key database, `runs/discovery/<run>/`, `runs/access-<id>/`.                                          |
| Package and install        | Approved commit, role mapping, per-engine allocation, host prerequisites.                                                  | `runs/deployment-env/<run>/`, remote `~/Narwhal-deploy/<id>/`, role files, fleet config, install marker.                          |
| Host and engine validation | PCI accelerator identity, visible devices, allocation, TP size, image identity, checkpoint tree digest, paths, and ports.  | Engine role env, launch record, discovery checkpoint manifests, `model_tree_sha256`, `ENGINE_RUN`, image-check and HTTP captures. |
| Fabric                     | Peer addresses, transport, representative process, live cache layout, workload budget.                                     | Cache layout, budget, link fingerprints, route files, directed samples, comparisons, edge matrix.                                 |
| Attestation                | Checked live process, protocol version, model dimensions, cache layout, transfer mode, handshake policy, sidecar endpoint. | `engine-attestation.json`, all `ENGINE_RUN` captures, router fleet before and after attestation.                                  |
| Profiling                  | Idle engines, fixed runtime, generated sequence limits, prompt lengths, and concurrency.                                   | Fleet config, profiling limits, profile and sample files.                                                                         |
| Preflight                  | Current processes, profiles, first-token calibration, fleet contract, and SLOs.                                            | Router environment, fleet config, calibration artifact, private check output.                                                     |
| Router                     | Bind address, model, engine count, and role split.                                                                         | Router env, fleet config, endpoint captures.                                                                                      |
| Capacity trial             | Workstation load client, router observability stack, SSH path, fixed runtime and cache policy, workload, and thresholds.   | Tunnel logs, Compose discovery, `runs/load-trial-<id>/`, manifests, request records, summaries, state and network snapshots.      |

Correction paths by gate:

| Gate                       | Typical correction path                                                                                                                                                                    |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Discovery and access       | Fix the named environment field, missing host utility, image inspection issue, credential, route, or independently verified changed host key.                                              |
| Package and install        | 1. Create a new prepared run from corrected input.<br>2. Resume dependency installation after fixing the host cause.                                                                       |
| Host and engine validation | 1. Restore device exposure or artifacts, free the planned ports, or repair the differing checkpoint file.<br>2. Repeat discovery.                                                          |
| Fabric                     | 1. Identify the root cause in route, binding, listener, firewall, HCA/GID, MTU, retransmissions or RDMA counters, CPU saturation, or concurrent traffic.<br>2. Re-sample.                  |
| Attestation                | 1. Inspect the bound capture and source.<br>2. Restart only the affected process or sidecar.<br>3. Rerun finalization.                                                                     |
| Profiling                  | 1. Correct the failing engine or sweep.<br>2. Write new samples to a fresh profile path.                                                                                                   |
| Preflight                  | Follow the reported engine, transfer leg, or budget into the matching troubleshooting path.                                                                                                |
| Router                     | Inspect the listener's process, the address family, the router error, and the upstream engine error.                                                                                       |
| Capacity trial             | 1. Resolve local port conflicts, remote listeners, scrape failures, client saturation, scheduler lag, and reconciliation gaps.<br>2. Attribute any remaining SLO miss to serving capacity. |

## Runtime and tooling references

<div class="grid cards" markdown>

-   [Configuration](Configuration.md)

    ---

    Fleet fields and defaults.

-   [Measure](Measure.md)

    ---

    Profiling and service-level objective (SLO) calibration.

-   [Observability](Observability.md)

    ---

    Prometheus and Grafana setup.

-   [Troubleshoot](Troubleshoot.md)

    ---

    Symptom-based diagnosis.

</div>

### External sources

| Project      | Reference                                                                                                                                               |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Narwhal      | [Environment template](https://github.com/athrael-soju/Narwhal/blob/main/.env.example)                                                                  |
| Hugging Face | [Snapshot download](https://huggingface.co/docs/huggingface_hub/guides/download)                                                                        |
| Hugging Face | [Model cards](https://huggingface.co/docs/hub/model-cards)                                                                                              |
| ROCm         | [Container device guidance](https://rocm.docs.amd.com/projects/install-on-linux/en/latest/how-to/docker.html)                                           |
| vLLM v0.29.0 | [NIXL connector usage](https://docs.vllm.ai/en/v0.29.0/features/nixl_connector_usage/)                                                                  |
| vLLM v0.29.0 | [Mamba layout resolver](https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/model_executor/layers/mamba/mamba_utils.py)                              |
| vLLM v0.29.0 | [KV cache interface](https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/v1/kv_cache_interface.py)                                                   |
| vLLM v0.29.0 | [KV cache grouping](https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/v1/core/kv_cache_utils.py)                                                   |
| vLLM v0.29.0 | [NIXL metadata and compatibility hash](https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/distributed/kv_transfer/kv_connector/v1/nixl/metadata.py) |
| vLLM v0.29.0 | [Model architecture conversion](https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/transformers_utils/model_arch_config_convertor.py)               |
| vLLM v0.29.0 | [KV cache layout enum](https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/v1/kv_cache_layout.py)                                                    |
| vLLM v0.29.0 | [NIXL worker](https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/distributed/kv_transfer/kv_connector/v1/nixl/base_worker.py)                       |
| vLLM v0.29.0 | [Attention backend utilities](https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/v1/attention/backends/utils.py)                                    |
| vLLM v0.29.0 | [NIXL connector aliases](https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/distributed/kv_transfer/kv_connector/v1/nixl/connector.py)              |
| iperf3       | [Command reference](https://software.es.net/iperf/invoking.html)                                                                                        |
| perftest     | [Repository](https://github.com/linux-rdma/perftest)                                                                                                    |
