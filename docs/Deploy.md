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

## Deployment sequence

Record each gate's result before the next gate.

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

| Change | Repeat |
| --- | --- |
| Offered rate or request count within the profiled range | Drain the router and run the next trial point with the existing profiles, fabric samples, and preflight. |
| SLO or first-token deadline | Run `narwhal-check` with the revised limits against saved profiles and live handoffs, then load the edited fleet. |
| Router restart with unchanged fleet configuration | Restart the router. |
| Engine restart with unchanged attested [`launch_digest`](configuration/01-Fleet-Schema.md#attestation) | Capture the live cache layout on vLLM or TensorRT-LLM and the attestation, then run full preflight against saved profiles and the [first-token calibration](deploy/06-Profile-and-Preflight.md#reusing-or-repeating-the-calibration). |
| Engine restart with changed attestation or only an attestation digest | Capture the live cache layout on vLLM or TensorRT-LLM and the attestation, [profile the new process generation](deploy/06-Profile-and-Preflight.md#reusing-or-repeating-profiles), [recalibrate the first-token deadline](deploy/06-Profile-and-Preflight.md#reusing-or-repeating-the-calibration), then run full preflight. |
| Changed cache geometry | Recalculate the fabric budget, from `cache-layout.json` on vLLM and TensorRT-LLM or from measured handoffs on SGLang. |
| Changed fabric route, host assignment, or transport | Measure affected directed links against the source budget, [recalibrate the first-token deadline](deploy/06-Profile-and-Preflight.md#reusing-or-repeating-the-calibration), and exercise the live KV paths in preflight. |

## Evidence and recovery index

Record these inputs and artifacts in the private deployment record:

| Stage | Inputs | Evidence to retain |
| --- | --- | --- |
| Discovery and access | `.env`, installed image and model, the checkpoint tree digest, GPU inspection utilities, launch policy, SSH route and key, the known-hosts file | `.env`, the files discovery writes under `config/`, `runs/discovery/<run>/`, `runs/access-<id>/` |
| Package and install | The wheel and its SHA-256, role mapping, per-engine allocation, host prerequisites | `runs/deployment-env/<run>/`, the remote `~/Narwhal-deploy/<id>/` directory |
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

