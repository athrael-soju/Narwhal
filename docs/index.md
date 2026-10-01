---
description: Narwhal hot-swaps prefill and decode roles across vLLM engines on NVIDIA and AMD GPUs, from a single GPU to multi-node fleets.
---

# Narwhal documentation

Narwhal is the first open-source LLM inference framework that automatically hot-swaps prefill and decode roles on both NVIDIA and AMD GPUs. It [moves engines between roles](Core-Concepts.md) as demand changes, on a fixed GPU fleet with model weights already loaded. It scales from a [single GPU](Dev-Runtime.md) to [distributed multi-node deployments](Deploy.md).

<div class="narwhal-hero">
  <img src="assets/social-preview.png" alt="The Narwhal logo, a black narwhal with a teal spiral tusk above the wordmark">
</div>

## What Narwhal provides

<div class="grid cards" markdown>

-   [Role hot-swap](Core-Concepts.md)

    ---

    Reassigns prefill and decode roles across a fixed GPU fleet, with NIXL key-value (KV) transfer between them.

-   [Serving](HTTP-API.md)

    ---

    Serves streaming and buffered completion and chat requests with latency-aware admission.

-   [Fault tolerance](Operate.md)

    ---

    Fails over to a warm-standby router and readmits engines against their live process generation.

-   [Measurement](Measure.md)

    ---

    Profiles engines and runs ordered benchmark points with retained evidence.

-   [Observability](Observability.md)

    ---

    Exports router and engine metrics to Prometheus and a provisioned Grafana dashboard.

-   [Operator tooling](CLI-Reference.md)

    ---

    Validates fleet files offline and collects private diagnostic bundles.

-   [Development mode](Dev-Runtime.md)

    ---

    Runs two to eight engines on one NVIDIA CUDA GPU under Ubuntu or WSL2.

</div>

## Getting started

Install the commands in a virtual environment on Linux with Python 3.11 or newer:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install narwhal-inference
narwhal --help
```

## Tasks

<div class="grid cards" markdown>

-   [Installing from PyPI](Install-from-PyPI.md)

    ---

    Install the Narwhal commands from PyPI and check their version.

-   [Narwhal dev](Dev-Runtime.md)

    ---

    Run a local NVIDIA GPU fleet on Ubuntu or WSL2.

-   [Deploying a fleet](Deploy.md)

    ---

    Bring up a new fleet and verify request routing.

-   [Measuring a fleet](Measure.md)

    ---

    Profile the fleet, select measurement ranges, calibrate SLOs, and measure a target workload.

-   [Setting up observability](Observability.md)

    ---

    Export metrics to Prometheus and inspect the fleet in Grafana.

-   [Operating Narwhal](Operate.md)

    ---

    Manage ingress, routers, engines, and software upgrades.

-   [Troubleshooting a fleet](Troubleshoot.md)

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
