# Narwhal documentation

Narwhal runs disaggregated LLM inference, splitting prefill and decode across separate engines. Narwhal reassigns prefill and decode roles among engines as load changes and keeps model weights loaded. See [Core concepts](Core-Concepts.md).

A management workstation runs deployments: it inspects the fleet, derives its configuration, prepares remote engines, and verifies request routing over the private deployment path.

<div class="narwhal-hero">
  <img src="assets/social-preview.png" alt="Narwhal project banner">
</div>

## Where to start

| Task                                                           | Page                                      |
| -------------------------------------------------------------- | ----------------------------------------- |
| Install the router commands from PyPI and check the version    | [Install from PyPI](Install-from-PyPI.md) |
| Run a local fleet on NVIDIA GPUs under Ubuntu or WSL2          | [Narwhal dev](Dev-Runtime.md)             |
| Bring up a new fleet and confirm request routing               | [Deploy a fleet](Deploy.md)               |
| Profile the fleet, pick measurement ranges, calibrate SLOs, and measure a target workload | [Measure a fleet](Measure.md)             |
| Send metrics to Prometheus and view the fleet in Grafana       | [Set up observability](Observability.md)  |
| Manage ingress, routers, engines, and upgrades                 | [Operate Narwhal](Operate.md)             |
| Diagnose a problem starting from a symptom                     | [Troubleshoot a fleet](Troubleshoot.md)   |

## Reference

| Page                                                  | Covers                                                                       |
| ----------------------------------------------------- | ---------------------------------------------------------------------------- |
| [Core concepts](Core-Concepts.md)                     | Engines, request placement, role control, and fleet state                    |
| [Configuration](Configuration.md)                     | Fleet configuration fields, environment inputs, and defaults                 |
| [CLI reference](CLI-Reference.md)                     | Commands, options, and exit codes                                            |
| [HTTP API reference](HTTP-API.md)                     | Completion, inspection, and lifecycle endpoints                              |
| [Telemetry and artifacts](Telemetry-and-Artifacts.md) | Journals, profiles, metrics, and the schema version of each persisted format |

## Working on Narwhal

[Contributing](https://github.com/athrael-soju/Narwhal/blob/main/CONTRIBUTING.md) covers development setup, running tests, and opening a pull request.
