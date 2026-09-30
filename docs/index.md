---
description: Narwhal hot-swaps prefill and decode roles across vLLM engines on NVIDIA and AMD GPUs, from a single GPU to multi-node fleets.
---

# Narwhal documentation

Narwhal is the first open-source LLM inference framework that automatically hot-swaps prefill and decode roles on both NVIDIA and AMD GPUs. It [moves engines between roles](Core-Concepts.md) as demand changes, on a fixed GPU fleet with model weights already loaded. It scales from a [single GPU](Dev-Runtime.md) to [distributed multi-node deployments](Deploy.md).

<div class="narwhal-hero">
  <img src="assets/social-preview.png" alt="The Narwhal logo, a black narwhal with a teal spiral tusk above the wordmark">
</div>

## What Narwhal provides

| Capability | Behavior | Guide |
| --- | --- | --- |
| Role hot-swap | Reassigns prefill and decode roles across a fixed GPU fleet, with NIXL key-value (KV) transfer between them. | [![Core concepts documentation](https://img.shields.io/badge/docs-Core%20concepts-0f766e)](Core-Concepts.md) |
| Serving | Serves streaming and buffered completion and chat requests with latency-aware admission. | [![HTTP API reference documentation](https://img.shields.io/badge/docs-HTTP%20API%20reference-0f766e)](HTTP-API.md) |
| Fault tolerance | Fails over to a warm-standby router and readmits engines against their live process generation. | [![Operate Narwhal documentation](https://img.shields.io/badge/docs-Operate%20Narwhal-0f766e)](Operate.md) |
| Measurement | Profiles engines and runs ordered benchmark points with retained evidence. | [![Measure a fleet documentation](https://img.shields.io/badge/docs-Measure%20a%20fleet-0f766e)](Measure.md) |
| Observability | Exports router and engine metrics to Prometheus and a provisioned Grafana dashboard. | [![Set up observability documentation](https://img.shields.io/badge/docs-Set%20up%20observability-0f766e)](Observability.md) |
| Operator tooling | Validates fleet files offline and collects private diagnostic bundles. | [![CLI reference documentation](https://img.shields.io/badge/docs-CLI%20reference-0f766e)](CLI-Reference.md) |
| Development mode | Runs two to eight engines on one NVIDIA CUDA GPU under Ubuntu or WSL2. | [![Narwhal dev documentation](https://img.shields.io/badge/docs-Narwhal%20dev-0f766e)](Dev-Runtime.md) |

## Get started

Install the commands in a virtual environment on Linux with Python 3.11 or newer:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install narwhal-inference
narwhal --help
```

## Tasks

<div class="grid cards" markdown>

-   [Install from PyPI](Install-from-PyPI.md)

    ---

    Install the Narwhal commands from PyPI and check their version.

-   [Narwhal dev](Dev-Runtime.md)

    ---

    Run a local NVIDIA GPU fleet on Ubuntu or WSL2.

-   [Deploy a fleet](Deploy.md)

    ---

    Bring up a new fleet and verify request routing.

-   [Measure a fleet](Measure.md)

    ---

    Profile the fleet, select measurement ranges, calibrate SLOs, and measure a target workload.

-   [Set up observability](Observability.md)

    ---

    Export metrics to Prometheus and inspect the fleet in Grafana.

-   [Operate Narwhal](Operate.md)

    ---

    Manage ingress, routers, engines, and software upgrades.

-   [Troubleshoot a fleet](Troubleshoot.md)

    ---

    Diagnose overload, engine failures, and router failures.

</div>

## Concepts and reference

<div class="grid cards" markdown>

-   [Core concepts](Core-Concepts.md)

    ---

    Engine, request placement, role control, and fleet state contracts.

-   [Configuration](Configuration.md)

    ---

    Fleet configuration fields, environment inputs, and their defaults.

-   [CLI reference](CLI-Reference.md)

    ---

    Commands, options, and exit codes.

-   [HTTP API reference](HTTP-API.md)

    ---

    Completion, inspection, and lifecycle endpoints.

-   [Telemetry and artifact reference](Telemetry-and-Artifacts.md)

    ---

    Journals, profiles, metrics, and persisted contract versions.

</div>

## Development

<div class="grid cards" markdown>

-   [Contributing](https://github.com/athrael-soju/Narwhal/blob/main/CONTRIBUTING.md)

    ---

    Development environment setup, the local test workflow, and the pull request process.

</div>
