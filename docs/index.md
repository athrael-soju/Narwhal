# Narwhal documentation

Narwhal serves LLM inference from a fixed GPU fleet and [shifts engines between prefill and decode roles](Core-Concepts.md) as demand changes, with model weights already loaded.

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

| Goal                                                                                           | Guide                                     |
| ---------------------------------------------------------------------------------------------- | ----------------------------------------- |
| Install the Narwhal commands from PyPI and check their version                                 | [Install from PyPI](Install-from-PyPI.md) |
| Run a local NVIDIA GPU fleet on Ubuntu or WSL2                                                 | [Narwhal dev](Dev-Runtime.md)             |
| Bring up a new fleet and verify request routing                                                | [Deploy a fleet](Deploy.md)               |
| Profile the fleet, select measurement ranges, calibrate SLOs, and measure a target workload    | [Measure a fleet](Measure.md)             |
| Export metrics to Prometheus and inspect the fleet in Grafana                                  | [Set up observability](Observability.md)  |
| Manage ingress, routers, engines, and software upgrades                                        | [Operate Narwhal](Operate.md)             |
| Diagnose overload, engine failures, or router failures starting from the first visible symptom | [Troubleshoot a fleet](Troubleshoot.md)   |

## Concepts and reference

- [Core concepts](Core-Concepts.md): engine, request placement, role control, and fleet state contracts.
- [Configuration](Configuration.md): fleet configuration fields, environment inputs, and their defaults.
- [CLI reference](CLI-Reference.md): commands, options, and exit codes.
- [HTTP API reference](HTTP-API.md): completion, inspection, and lifecycle endpoints.
- [Telemetry and artifact reference](Telemetry-and-Artifacts.md): journals, profiles, metrics, and persisted contract versions.

## Development

- [Contributing](https://github.com/athrael-soju/Narwhal/blob/main/CONTRIBUTING.md): development environment setup, the local test workflow, and the pull request process.
