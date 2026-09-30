# Narwhal documentation

Narwhal is the first open-source LLM inference framework that automatically hot-swaps prefill and decode roles on both NVIDIA and AMD GPUs. It [moves engines between roles](Core-Concepts.md) as demand changes, on a fixed GPU fleet with model weights already loaded. It scales from a [single GPU](Dev-Runtime.md) to [distributed multi-node deployments](Deploy.md).

<div class="narwhal-hero">
  <img src="assets/social-preview.png" alt="The Narwhal logo, a black narwhal with a teal spiral tusk above the wordmark">
</div>

## What Narwhal provides

| Capability | Behavior | Guide |
| --- | --- | --- |
| Role hot-swap | Reassigns prefill and decode roles across a fixed GPU fleet. | [Core concepts](Core-Concepts.md) |
| Split routing | Routes prefill and decode separately with NIXL key-value (KV) transfer. | [Core concepts](Core-Concepts.md) |
| Latency-aware admission | Admits and places requests from measured per-engine profiles. | [Measure a fleet](Measure.md) |
| Completion APIs | Serves streaming and buffered completion and chat requests. | [HTTP API reference](HTTP-API.md) |
| Router failover | Promotes a warm-standby router. | [Operate Narwhal](Operate.md) |
| Observability | Exports Prometheus metrics and request journals to a Grafana dashboard. | [Set up observability](Observability.md) |
| Generation-bound readmission | Readmits an engine only against its live process generation. | [Restart engines](operate/03-Restart-Engines.md) |
| Offline validation | Validates fleet files and collects diagnostic bundles. | [`narwhal config`](Config-Inspection.md), [`narwhal diagnostics`](Diagnostic-Bundles.md) |
| Benchmark runs | Runs ordered benchmark points with retained evidence. | [Ordered benchmark points](measure/05-Benchmark-Runner.md) |
| Development mode | Runs four engines on one NVIDIA CUDA GPU under Ubuntu or WSL2. | [Narwhal dev](Dev-Runtime.md) |

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

    Diagnose overload, engine failures, or router failures starting from the first visible symptom.

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
