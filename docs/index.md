# Narwhal documentation

Narwhal runs disaggregated LLM inference, splitting prefill and decode across separate engines. Those roles aren't fixed. As demand shifts, Narwhal [reassigns them](Core-Concepts.md), and the engines keep their model weights loaded the whole time.

You drive a deployment from a management workstation. From there, Narwhal inspects the fleet, works out its configuration, prepares the remote engines, and checks that inference is being routed correctly over the private deployment path.

<div class="narwhal-hero">
  <img src="assets/social-preview.png" alt="Narwhal, adaptive disaggregated inference on a role-free fleet">
</div>

## Where to start

| If you want to...                                                                         | Read                                      |
| ----------------------------------------------------------------------------------------- | ----------------------------------------- |
| Install the router commands from PyPI and check the version                               | [Install from PyPI](Install-from-PyPI.md) |
| Run a local fleet on NVIDIA GPUs under Ubuntu or WSL2                                     | [Narwhal dev](Dev-Runtime.md)             |
| Bring up a new fleet and confirm requests are routed correctly                            | [Deploy a fleet](Deploy.md)               |
| Profile the fleet, pick measurement ranges, calibrate SLOs, and measure a target workload | [Measure a fleet](Measure.md)             |
| Send metrics to Prometheus and look at the fleet in Grafana                               | [Set up observability](Observability.md)  |
| Manage ingress, routers, engines, and upgrades                                            | [Operate Narwhal](Operate.md)             |
| Work out what's wrong from the first symptom you see                                      | [Troubleshoot a fleet](Troubleshoot.md)   |

## Reference

| Page                                                  | What it covers                                                                |
| ----------------------------------------------------- | ----------------------------------------------------------------------------- |
| [Core concepts](Core-Concepts.md)                     | How engines, request placement, role control, and fleet state fit together    |
| [Configuration](Configuration.md)                     | Every fleet configuration field, the environment inputs, and their defaults   |
| [CLI reference](CLI-Reference.md)                     | Commands, options, and exit codes                                             |
| [HTTP API reference](HTTP-API.md)                     | Completion, inspection, and lifecycle endpoints                               |
| [Telemetry and artifacts](Telemetry-and-Artifacts.md) | Journals, profiles, metrics, and the schema version of each persisted format  |

## Working on Narwhal itself

If you want to build or change Narwhal, start with [Contributing](https://github.com/athrael-soju/Narwhal/blob/main/CONTRIBUTING.md). It explains how to set up a development environment, run the tests locally, and open a pull request.
