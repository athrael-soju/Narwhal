# Narwhal fleet deployment runbook

This runbook is for an operator bringing up a Narwhal fleet for the first time from a management workstation. The objective is to prove, in order, that the inspected hardware and model are the ones being deployed, the approved source revision is installed, each engine satisfies its runtime contract, every required KV-transfer path has enough measured capacity, the fleet can transfer KV between live peers, and the router can sustain a measured workload through the private management path.

Prometheus and Grafana run on the router host for the initial trial.

## Operating model

A fleet serves one model with a compatible KV layout across vLLM engines using NIXL with effective `kv_both` behaviour. Every eligible KV producer must be able to transfer to every eligible consumer. Exactly one controller is active for the fleet.

Three machine roles are involved:

| Role                          | Responsibilities                                                                                                                                                                           | Inputs that must already exist                                                                                                                                         |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Management workstation        | Hold credentials, generate private configuration, inspect remote hosts, package the approved source revision, drive installation, open role shells, tunnel trial traffic, retain evidence. | Private `.env`, approved source revision, engine image, model name and paths, fabric interface, run directory, service ports, management destinations and credentials. |
| Router and observability host | Hold the fleet configuration and profiles, run Narwhal checks and router, host Prometheus and Grafana.                                                                                     | Verified source bundle and revision, engine and attestation URLs, API credential, model and SLO settings, Docker Engine and Compose plugin.                            |
| Engine hosts                  | Expose accelerators and transfer devices, run vLLM and attestation sidecars, provide cache and transfer evidence.                                                                          | Accelerator allocation, TP shape, pinned image, checkpoint, launch policy, fabric addresses and ports.                                                                 |

The management workstation needs Git, Bash, Python 3.11 or newer, OpenSSH, and the supplied access tooling. Any remote host that runs Narwhal commands needs Python 3.11 or newer with `venv`, Git, Make, and curl. Engine hosts also need the configured accelerator driver, container runtime, and transfer devices. Password-based SSH additionally requires `sshpass` on the workstation.

Inventory roles describe deployment responsibilities. Hardware discovered on the management workstation applies only to that workstation.

### Concurrency rules

Parallelise work only where the evidence remains attributable to one host, process, or link.

- After installation, keep one shell per physical engine host and inspect hosts concurrently.
- Start one serving representative for each distinct cache configuration. Representatives on different hosts may load concurrently.
- Measure fabric one directed edge at a time.
- After fabric qualification, start and attest remaining engines concurrently only where GPU allocations do not overlap.
- Colocated roles that overlap GPU allocations must be serialised.

### Deployment record

Create a private deployment record before the first command. For every gate, retain:

- host or stable host alias;
- full source revision;
- starting state;
- commands executed;
- exit status;
- generated artifacts and paths.

Record a blocked gate before modifying the failed state. When recovering a failed attempt, retain its evidence and remove only files and processes created by that attempt. A passing Gate G hands the running engines, attestation sidecars, router, and monitoring stack to the operator for continued service. When evidence leaves the private environment, replace private addresses, paths, and credentials with stable aliases.

## Deployment sequence

Follow each gate in order. Record its result before entering the next gate.

1. [Gate A: Freeze inputs and discover the real deployment](deploy/01-Discover.md)
2. [Gate B: Package and install the approved revision](deploy/02-Install.md)
3. [Gate C: Prove each host and one engine per cache class](deploy/03-Validate-Engines.md)
4. [Gate D: Prove the transfer fabric against the serving cache](deploy/04-Qualify-Fabric.md)
5. [Gate E: Expand the fleet and attest the exact live processes](deploy/05-Attest.md)
6. [Gate F: Characterise performance and run the live KV contract](deploy/06-Profile-and-Preflight.md)
7. [Gate G: Start the service and validate capacity through the private path](deploy/07-Serve-and-Measure.md)

## Evidence and recovery index

Use this table to decide what must be preserved before changing a failed state.

| Gate                       | Inputs that define the gate                                                                                                | Typical correction path                                                                                                                                                         | Evidence to retain                                                                                                                      |
| -------------------------- | -------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| Discovery and access       | `.env`, installed image and model, GPU inspection utilities, launch policy, SSH route and key.                             | Fix named environment field, missing host utility, image inspection issue, credential or route. Verify changed host keys independently.                                         | `.env`, generated JSON, host-key database, `runs/discovery/<run>/`, `runs/access-<id>/`.                                                |
| Package and install        | Approved commit, role mapping, per-engine allocation, host prerequisites.                                                  | Correct preparation input and create a new prepared run. Resume dependency installation only after host cause is fixed.                                                         | `runs/deployment-env/<run>/`, remote `~/Narwhal-deploy/<id>/`, role files, fleet config, install marker.                                |
| Host and engine validation | PCI accelerator identity, visible devices, allocation, TP size, image identity, checkpoint tree digest, paths and ports.   | Restore device exposure or artifacts; resolve listener ownership; for checkpoint mismatch repair the differing file and repeat discovery.                                       | Engine role env and launch record; discovery checkpoint manifests and `model_tree_sha256`; `ENGINE_RUN`, image-check and HTTP captures. |
| Fabric                     | Peer addresses, transport, representative process, live cache layout, workload budget.                                     | Diagnose route, binding, listener, firewall, HCA/GID, MTU, retransmissions or RDMA counters, CPU saturation, concurrent traffic. Re-sample only after root cause is identified. | Cache layout, budget, link fingerprints, route files, directed samples, comparisons, edge matrix.                                       |
| Fleet expansion            | Qualified host/fabric state, pinned image and packages, cache shape, device allocation.                                    | Repeat checked launch procedure for the affected role;                                                                                                                          | Fleet config, role env, launch record, helper digests, container ID/logs, HTTP captures.                                                |
| Attestation                | Checked live process, protocol version, model dimensions, cache layout, transfer mode, handshake policy, sidecar endpoint. | Inspect the bound capture and source; restart only affected process or sidecar as required, then rerun finalisation.                                                            | `engine-attestation.json`, all `ENGINE_RUN` captures, router fleet before/after attestation.                                            |
| Profiling                  | Idle engines, unchanged runtime, generated sequence limits, prompt lengths and concurrency.                                | Correct the failing engine or sweep; write new samples to a fresh profile path. Restart means reprofile.                                                                        | Fleet config, profiling limits, profile and sample files.                                                                               |
| Preflight                  | Current processes, profiles, fleet contract and SLOs.                                                                      | Follow the reported engine, transfer leg, or budget into the matching troubleshooting path.                                                                                     | Router environment, fleet config, private check output.                                                                                 |
| Router                     | Bind address, model, engine count and role split.                                                                          | Inspect listener ownership, address family, router error, or upstream engine error.                                                                                             | Router env, fleet config, endpoint captures.                                                                                            |
| Capacity trial             | Workstation load client, router observability stack, SSH path, fixed runtime/cache policy, workload and thresholds.        | Resolve local port conflicts, remote listeners, scrape failures, client saturation or scheduler lag; reconcile records before attributing an SLO miss to serving capacity.      | Tunnel logs, Compose discovery, `runs/load-trial-<id>/`, manifests, request records, summaries, state/network snapshots.                |

## References used by the procedure

The commands and compatibility rules in the linked gates rely on the project documentation and pinned upstream behaviour referenced by the deployment tooling, including `.env.example`, `Configuration.md`, `Measure.md`, `Observability.md`, `Troubleshoot.md`, Hugging Face snapshot/model-card documentation, ROCm container guidance, vLLM v0.29.0 NIXL and cache-layout sources, iperf3 documentation, and perftest documentation.

### External implementation references

- Narwhal environment template: https://github.com/athrael-soju/Narwhal/blob/main/.env.example
- Hugging Face snapshot download: https://huggingface.co/docs/huggingface_hub/guides/download
- Hugging Face model cards: https://huggingface.co/docs/hub/model-cards
- ROCm container device guidance: https://rocm.docs.amd.com/projects/install-on-linux/en/latest/how-to/docker.html
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
- iperf3 command reference: https://software.es.net/iperf/invoking.html
- perftest: https://github.com/linux-rdma/perftest
