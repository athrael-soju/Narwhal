# Narwhal fleet deployment runbook

This runbook takes a Narwhal fleet from bare hosts to a running, measured service. Discovery records the hardware, model, and launch inputs, and installation puts the approved revision on each host. You then start each engine from a checked plan, capture its live cache layout, and measure the fabric between hosts while nothing else is running. After that, you [attest](deploy/05-Attest.md) and profile the engines, and you start the router once preflight passes. For the initial trial, Prometheus and Grafana run on the router host.

## Operating model

A fleet serves one model, and every engine in it uses a compatible KV layout. The engines run vLLM with NIXL and act as both KV producers and consumers (`kv_both`). Every eligible producer must be able to send KV to every eligible consumer, and only one controller may be active for a fleet at any time.

Three kinds of machine are involved:

| Role                          | Responsibilities                                                                                                                                                                           | Inputs that must already exist                                                                                                                                         |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Management workstation        | Hold credentials, generate private configuration, inspect remote hosts, package the approved source revision, drive installation, open role shells, tunnel trial traffic, retain evidence. | Private `.env`, approved source revision, engine image, model name and paths, fabric interface, run directory, service ports, management destinations and credentials. |
| Router and observability host | Hold the fleet configuration and profiles, run Narwhal checks and router, host Prometheus and Grafana.                                                                                     | Verified source bundle and revision, engine and attestation URLs, API credential, model and SLO settings, Docker Engine and Compose plugin.                            |
| Engine hosts                  | Expose accelerators and transfer devices, run vLLM and attestation sidecars, provide cache and transfer evidence.                                                                          | Accelerator allocation, TP shape, pinned image, checkpoint, launch policy, fabric addresses and ports.                                                                 |

The management workstation needs Git, Bash, Python 3.11 or newer, OpenSSH, and the supplied access tooling, plus `sshpass` if you log in to hosts with passwords. Every remote host that runs Narwhal commands needs Python 3.11 or newer with `venv`, Git, Make, and curl. Engine hosts also need the accelerator driver, a container runtime, and the transfer devices.

GPU inventory has to come from the engine host itself. Running the inspection on the management workstation only tells you about the workstation's own hardware.

### What can run in parallel

- After installation, keep one shell open per physical engine host and inspect the hosts at the same time.
- Engines whose GPU allocations don't overlap can be started together, each capturing its own cache layout. Roles that share GPUs must start one after another.
- Measure the fabric one directed link at a time, with all engines idle.
- After fabric qualification, attest the running engines in parallel.

### Keeping a deployment record

Start a private deployment record before you run the first command. For each gate, write down:

- the host, or a stable alias for it;
- the full source revision;
- the state you started from;
- the commands you ran and their exit status;
- the artifacts they produced and where they are.

If a gate is blocked, record that before you change anything. During recovery, keep the evidence from the failed attempt, and clean up only the files and processes that attempt created.

When you export evidence from the private environment, replace private addresses, paths, and credentials with stable aliases.

## Deployment sequence

Work through the gates in order, and record each result before moving on.

1. [Gate A: Freeze inputs and discover the real deployment](deploy/01-Discover.md)
2. [Gate B: Package and install the approved revision](deploy/02-Install.md)
3. [Gate C: Validate and start every engine](deploy/03-Validate-Engines.md)
4. [Gate D: Prove the transfer fabric against the serving cache](deploy/04-Qualify-Fabric.md)
5. [Gate E: Attest the live engine processes](deploy/05-Attest.md)
6. [Gate F: Profile the engines and run the live KV contract](deploy/06-Profile-and-Preflight.md)
7. [Gate G: Start the service and validate capacity through the private path](deploy/07-Serve-and-Measure.md)

When Gate G is done, the engines, attestation sidecars, router, and monitoring stack stay running as the live service.

## What to repeat after a change

Each gate's evidence depends on specific inputs. When one of them changes, repeat the work listed here and nothing more. The individual gates link back to this table instead of restating it.

| Change                                                                                     | Work to repeat                                                                                                                                                                                                                       |
| ------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Hardware, container image, or checkpoint contents                                          | Run discovery again into a new output directory, prepare a new deployment run, and install it. Validate engine 1 before updating the rest, then continue through the gates.                                                          |
| Model dtype, cache policy, TP allocation, or model arguments                               | Change the `.env` input, run discovery again into a new output directory, and prepare and install a new deployment run. Then regroup the engines, check a new launch plan, and capture a new serving layout (Gate C).                |
| The engine launcher script                                                                 | Prepare a new deployment run, so the deployed snapshot and its digest match the management checkout.                                                                                                                                 |
| Resolved cache layout or page geometry                                                     | Recalculate the fabric budget from the new `cache-layout.json` (Gate D).                                                                                                                                                             |
| Workload assumptions: handoff rate, prompt size, burst, transfer budget, or headroom       | Recalculate the fabric budget.                                                                                                                                                                                                       |
| Fabric route, host assignment, interface, transport, test tool version, or test parameters | Measure the affected directed links again against the source budget, [recalibrate the first-token deadline](deploy/06-Profile-and-Preflight.md#calibrate-the-first-token-deadline), and run preflight to exercise the live KV paths. |
| Model, engine generation, or served context length                                         | [Recalibrate the first-token deadline](deploy/06-Profile-and-Preflight.md#calibrate-the-first-token-deadline) into a new artifact, update the fleet configuration, and copy both to every router host.                               |
| Engine restart with the same launch plan                                                   | Capture its live cache and attestation, profile the new generation, recalibrate the first-token deadline, and run full preflight. If the captured geometry changed, recalculate its fabric budget too.                               |
| Router restart with the same fleet document                                                | Nothing by hand. Before accepting traffic, the router checks saved profiles and the configured first-token calibration against the live engine generations.                                                                          |
| SLO targets or first-token deadline                                                        | Run `narwhal-check`, which tests the new limits against saved profiles and live handoffs before the router loads the edited fleet.                                                                                                   |
| Offered rate or request count, within the profiled workload range                          | Drain the router and run the next trial point. The engine profiles, fabric samples, and full preflight all still hold.                                                                                                               |

## Evidence and recovery index

| Gate                       | Inputs that define the gate                                                                                                | Typical correction path                                                                                                                                                      | Evidence to retain                                                                                                                      |
| -------------------------- | -------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| Discovery and access       | `.env`, installed image and model, GPU inspection utilities, launch policy, SSH route and key.                             | Fix the named environment field, missing host utility, image inspection issue, credential or route. Verify changed host keys independently.                                  | `.env`, generated JSON, host-key database, `runs/discovery/<run>/`, `runs/access-<id>/`.                                                |
| Package and install        | Approved commit, role mapping, per-engine allocation, host prerequisites.                                                  | Correct the preparation input and create a new prepared run. Resume dependency installation only once the host problem is fixed.                                             | `runs/deployment-env/<run>/`, remote `~/Narwhal-deploy/<id>/`, role files, fleet config, install marker.                                |
| Host and engine validation | PCI accelerator identity, visible devices, allocation, TP size, image identity, checkpoint tree digest, paths and ports.   | Restore device exposure or artifacts; resolve listener ownership. For a checkpoint mismatch, repair the differing file and run discovery again.                              | Engine role env and launch record; discovery checkpoint manifests and `model_tree_sha256`; `ENGINE_RUN`, image-check and HTTP captures. |
| Fabric                     | Peer addresses, transport, representative process, live cache layout, workload budget.                                     | Diagnose route, binding, listener, firewall, HCA/GID, MTU, retransmissions or RDMA counters, CPU saturation, competing traffic. Re-sample only once the root cause is known. | Cache layout, budget, link fingerprints, route files, directed samples, comparisons, edge matrix.                                       |
| Attestation                | Checked live process, protocol version, model dimensions, cache layout, transfer mode, handshake policy, sidecar endpoint. | Inspect the failing capture and its source. Restart only the affected process or sidecar, then run finalization again.                                                       | `engine-attestation.json`, all `ENGINE_RUN` captures, router fleet before and after attestation.                                        |
| Profiling                  | Idle engines, unchanged runtime, generated sequence limits, prompt lengths and concurrency.                                | Fix the failing engine or sweep, and write new samples to a new profile path.                                                                                                | Fleet config, profiling limits, profile and sample files.                                                                               |
| Preflight                  | Current processes, profiles, first-token calibration, fleet contract and SLOs.                                             | Follow the reported engine, transfer leg or budget into the matching troubleshooting section.                                                                                | Router environment, fleet config, calibration artifact, private check output.                                                           |
| Router                     | Bind address, model, engine count and role split.                                                                          | Check listener ownership, address family, router errors and upstream engine errors.                                                                                          | Router env, fleet config, endpoint captures.                                                                                            |
| Capacity trial             | Workstation load client, router observability stack, SSH path, fixed runtime and cache policy, workload and thresholds.    | Resolve local port conflicts, remote listeners, scrape failures, client saturation or scheduler lag. Reconcile records before blaming an SLO miss on serving capacity.       | Tunnel logs, Compose discovery, `runs/load-trial-<id>/`, manifests, request records, summaries, state and network snapshots.            |

## Runtime and tooling references

While you work through the gates, keep [Configuration](Configuration.md), [Measure](Measure.md), [Observability](Observability.md), and [Troubleshoot](Troubleshoot.md) on hand. The external sources below cover the environment fields, model downloads, ROCm container devices, vLLM v0.29.0 KV layout and NIXL behavior, and the fabric test tools.

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
