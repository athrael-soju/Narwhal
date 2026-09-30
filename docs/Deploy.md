# Deploy a fleet

## Operating model

Fleet requirements:

- one model on vLLM engines that share a compatible KV layout;
- KV transfer through NIXL with effective `kv_both` behaviour;
- transfer from every eligible KV producer to every eligible consumer;
- one active role controller per fleet.

| Role                          | Responsibilities                                                                                                                                                                           | Inputs that must already exist                                                                                                                                         |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Management workstation        | Hold credentials, generate private configuration, inspect remote hosts, package the approved source revision, drive installation, open role shells, tunnel trial traffic, retain evidence. | Private `.env`, approved source revision, engine image, model name and paths, fabric interface, run directory, service ports, management destinations and credentials. |
| Router and observability host | Hold the fleet configuration and profiles, run Narwhal checks and router, host Prometheus and Grafana.                                                                                     | Verified source bundle and revision, engine and attestation URLs, API credential, model and SLO settings, Docker Engine and Compose plugin.                            |
| Engine hosts                  | Expose accelerators and transfer devices, run vLLM and attestation sidecars, provide cache and transfer evidence.                                                                          | Accelerator allocation, TP shape, pinned image, checkpoint, launch policy, fabric addresses and ports.                                                                 |

| Machine                                | Required software                                                                                            |
| -------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| Management workstation                 | Git, Bash, Python 3.11 or newer, OpenSSH, the supplied access tooling, and `sshpass` for password-based SSH. |
| Remote host that runs Narwhal commands | Python 3.11 or newer with `venv`, Git, Make, and curl.                                                       |
| Engine host                            | The remote host software plus the configured accelerator driver, container runtime, and transfer devices.   |

### Concurrency rules

Checks per engine and host:

- Compare each engine allocation with the GPU inventory collected on that engine host.
- Keep one installed role shell per physical engine host.
- Capture each engine's live cache layout.

Concurrent work:

- Inspect engine hosts concurrently.
- Start engines with disjoint GPU allocations concurrently.
- Attest the running engines concurrently after fabric qualification.

One at a time:

- Start engines that share GPUs one at a time.
- Measure fabric one directed edge at a time while all engines are idle.

### Deployment record

Create a private deployment record before the first command.

For every gate, retain:

- host or stable host alias;
- full source revision;
- starting state;
- commands executed;
- exit status;
- generated artifacts and paths.

When a gate fails:

1. Record the failed state before changing it.
2. Retain the failed attempt's evidence.
3. Remove only the files and processes the failed attempt created.

Replace private addresses, paths, and credentials with stable aliases before exporting evidence from the private environment.

## Deployment sequence

Record each gate's result before the next gate.

1. [Gate A: Freeze inputs and discover the deployment](deploy/01-Discover.md)
2. [Gate B: Package and install the approved revision](deploy/02-Install.md)
3. [Gate C: Validate and start every engine](deploy/03-Validate-Engines.md)
4. [Gate D: Prove the transfer fabric against the serving cache](deploy/04-Qualify-Fabric.md)
5. [Gate E: Attest the live engine processes](deploy/05-Attest.md)
6. [Gate F: Profile once and run the live KV contract](deploy/06-Profile-and-Preflight.md)
7. [Gate G: Start the service and validate capacity through the private path](deploy/07-Serve-and-Measure.md)

Work to repeat by change:

| Change                                                           | Work to repeat                                                                                                                                                                                                                                                                              |
| ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Offered rate or request count within the profiled workload range | 1. Drain the router.<br>2. Run the next trial point on the existing engine profiles, fabric samples, and full preflight.                                                                                                                                                                          |
| SLO or first-token deadline                                      | 1. Run `narwhal-check` with the revised limits against the saved profiles and live handoffs.<br>2. Load the edited fleet in the router.                                                                                                                                                          |
| Router restart with the same fleet configuration                 | Restart the router.                                                                                                                                                                                                                                                                         |
| Engine restart with the same launch plan                         | 1. Capture its live cache layout and attestation.<br>2. Profile the new process generation.<br>3. [Recalibrate the first-token deadline](deploy/06-Profile-and-Preflight.md#calibrate-the-first-token-deadline).<br>4. Run full preflight.                                                                  |
| Cache geometry change after an engine restart                    | Recalculate the engine's fabric budget from the new `cache-layout.json`.                                                                                                                                                                                                                    |
| Fabric route, host assignment, or transport                      | 1. Measure the affected directed links against the source budget.<br>2. [Recalibrate the first-token deadline](deploy/06-Profile-and-Preflight.md#calibrate-the-first-token-deadline).<br>3. Exercise the live KV paths in preflight.                                                                  |

## Evidence and recovery index

| Gate                       | Inputs that define the gate                                                                                                | Typical correction path                                                                                                                                                               | Evidence to retain                                                                                                                      |
| -------------------------- | -------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| Discovery and access       | `.env`, installed image and model, GPU inspection utilities, launch policy, SSH route and key.                             | Fix the named environment field, missing host utility, image inspection issue, credential, route, or independently verified changed host key.                                        | `.env`, generated JSON, host-key database, `runs/discovery/<run>/`, `runs/access-<id>/`.                                                |
| Package and install        | Approved commit, role mapping, per-engine allocation, host prerequisites.                                                  | 1. Create a new prepared run from corrected input.<br>2. Resume dependency installation once the host cause is fixed.                                                                     | `runs/deployment-env/<run>/`, remote `~/Narwhal-deploy/<id>/`, role files, fleet config, install marker.                                |
| Host and engine validation | PCI accelerator identity, visible devices, allocation, TP size, image identity, checkpoint tree digest, paths, and ports.  | 1. Restore device exposure or artifacts, free the planned ports, or repair the differing checkpoint file.<br>2. Repeat discovery.                                                   | Engine role env, launch record, discovery checkpoint manifests, `model_tree_sha256`, `ENGINE_RUN`, image-check and HTTP captures.       |
| Fabric                     | Peer addresses, transport, representative process, live cache layout, workload budget.                                     | 1. Identify the root cause in route, binding, listener, firewall, HCA/GID, MTU, retransmissions or RDMA counters, CPU saturation, or concurrent traffic.<br>2. Re-sample.           | Cache layout, budget, link fingerprints, route files, directed samples, comparisons, edge matrix.                                       |
| Attestation                | Checked live process, protocol version, model dimensions, cache layout, transfer mode, handshake policy, sidecar endpoint. | 1. Inspect the bound capture and source.<br>2. Restart only the affected process or sidecar.<br>3. Rerun finalization.                                                                           | `engine-attestation.json`, all `ENGINE_RUN` captures, router fleet before/after attestation.                                            |
| Profiling                  | Idle engines, unchanged runtime, generated sequence limits, prompt lengths, and concurrency.                               | 1. Correct the failing engine or sweep.<br>2. Write new samples to a fresh profile path.                                                                                              | Fleet config, profiling limits, profile and sample files.                                                                               |
| Preflight                  | Current processes, profiles, first-token calibration, fleet contract, and SLOs.                                            | Follow the reported engine, transfer leg, or budget into the matching troubleshooting path.                                                                                           | Router environment, fleet config, calibration artifact, private check output.                                                           |
| Router                     | Bind address, model, engine count, and role split.                                                                         | Inspect which process holds the listener, the address family, router error, or upstream engine error.                                                                                 | Router env, fleet config, endpoint captures.                                                                                            |
| Capacity trial             | Workstation load client, router observability stack, SSH path, fixed runtime/cache policy, workload, and thresholds.       | 1. Resolve local port conflicts, remote listeners, scrape failures, client saturation, scheduler lag, and unreconciled records.<br>2. Attribute any remaining SLO miss to serving capacity.     | Tunnel logs, Compose discovery, `runs/load-trial-<id>/`, manifests, request records, summaries, state/network snapshots.                |

## Runtime and tooling references

- [Configuration](Configuration.md): fleet fields and defaults.
- [Measure](Measure.md): profiling and service-level objective (SLO) calibration.
- [Observability](Observability.md): Prometheus and Grafana setup.
- [Troubleshoot](Troubleshoot.md): symptom-based diagnosis.

External sources:

Narwhal and Hugging Face:

- Narwhal environment template: https://github.com/athrael-soju/Narwhal/blob/main/.env.example
- Hugging Face snapshot download: https://huggingface.co/docs/huggingface_hub/guides/download
- Hugging Face model cards: https://huggingface.co/docs/hub/model-cards

ROCm:

- ROCm container device guidance: https://rocm.docs.amd.com/projects/install-on-linux/en/latest/how-to/docker.html

vLLM:

- vLLM NIXL connector usage, v0.29.0: https://docs.vllm.ai/en/v0.29.0/features/nixl_connector_usage/
- vLLM Mamba layout resolver, v0.29.0: https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/model_executor/layers/mamba/mamba_utils.py
- vLLM KV cache interface, v0.29.0: https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/v1/kv_cache_interface.py
- vLLM KV cache grouping, v0.29.0: https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/v1/core/kv_cache_utils.py
- vLLM NIXL metadata and compatibility hash, v0.29.0: https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/distributed/kv_transfer/kv_connector/v1/nixl/metadata.py
- vLLM model architecture conversion, v0.29.0: https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/transformers_utils/model_arch_config_convertor.py
- vLLM KV cache layout enum, v0.29.0: https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/v1/kv_cache_layout.py
- vLLM NIXL worker, v0.29.0: https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/distributed/kv_transfer/kv_connector/v1/nixl/base_worker.py
- vLLM attention backend utilities, v0.29.0: https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/v1/attention/backends/utils.py
- vLLM NIXL connector aliases, v0.29.0: https://github.com/vllm-project/vllm/blob/v0.29.0/vllm/distributed/kv_transfer/kv_connector/v1/nixl/connector.py

Network measurement:

- iperf3 command reference: https://software.es.net/iperf/invoking.html
- perftest: https://github.com/linux-rdma/perftest
