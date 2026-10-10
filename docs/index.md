---
description: Narwhal documentation for development, fleet deployment, operation, and reference.
---

# Narwhal documentation

<div class="narwhal-hero">
  <img src="assets/social-preview.png" alt="The Narwhal logo, a black narwhal with a teal spiral tusk above the wordmark">
</div>

Narwhal is a Python framework for disaggregated LLM inference. It assigns prefill and decode roles across dual-capability engines as demand changes.

The [README](https://github.com/athrael-soju/Narwhal#what-is-narwhal) defines the product contract.

## Run Narwhal

<div class="grid cards" markdown>

-   [Install Narwhal](Install-from-PyPI.md)

    ---

    Install the wheel and run the installed commands.

-   [Run Narwhal on one GPU](Dev-Runtime.md)

    ---

    Install the CUDA runtime, start a local fleet, and qualify it.

-   [Deploy a fleet](Deploy.md)

    ---

    Take a fleet from host discovery through serving and capacity validation.

-   [Operate Narwhal](Operate.md)

    ---

    Monitor routers, restart engines, upgrade the deployment, and control the fleet.

-   [Measure a fleet](Measure.md)

    ---

    Profile engines, select targets, and run load and benchmark trials.

-   [Set up observability](Observability.md)

    ---

    Export metrics to Prometheus and read the Grafana dashboard.

-   [Troubleshoot a fleet](Troubleshoot.md)

    ---

    Diagnose overload, engine failures, and router failures.

</div>

## Reference

<div class="grid cards" markdown>

-   [Core concepts](Core-Concepts.md)

    ---

    Request flow, topology, role control, failure, and state contracts.

-   [Configuration](Configuration.md)

    ---

    Fleet fields, deployment inputs, and validation rules.

-   [CLI](CLI-Reference.md)

    ---

    Commands, options, results, and exit codes.

-   [HTTP API](HTTP-API.md)

    ---

    Completion, inspection, live-state, and lifecycle routes.

-   [Telemetry and artifacts](Telemetry-and-Artifacts.md)

    ---

    Journal, profile, metric, failure, and compatibility contracts.

</div>

Read the [contributor guide](https://github.com/athrael-soju/Narwhal/blob/main/CONTRIBUTING.md), [code of conduct](https://github.com/athrael-soju/Narwhal/blob/main/CODE_OF_CONDUCT.md), and [security policy](https://github.com/athrael-soju/Narwhal/blob/main/SECURITY.md) before contributing.
