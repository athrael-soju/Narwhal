<p align="center">
  <img src="../assets/social-preview.png" alt="Narwhal logo and wordmark" width="100%">
</p>

# Narwhal documentation

[Deploy a fleet](Deploy.md) starts on your management workstation with the supplied private inventory and access, then takes you through remote host preparation, engine launch, KV transfer checks and a routed completion. Continue through the private-route workload trial, then verify supervised restart and readmission.

## Deployment and operations

| Guide | Use it for |
| --- | --- |
| [Deploy a fleet](Deploy.md) | Set up the remote engines and router, then validate the deployment. |
| [Measure a fleet](Measure.md) | Select profiling ranges, calibrate SLOs and measure the deployment workload. |
| [Set up observability](Observability.md) | Configure Prometheus targets and inspect the Grafana dashboard. |
| [Operate Narwhal](Operate.md) | Configure ingress, supervise routers, maintain engines and perform upgrades. |
| [Troubleshoot a fleet](Troubleshoot.md) | Diagnose overload, engine faults and router faults from their first signal. |

## Architecture and references

| Reference | Use it for |
| --- | --- |
| [Core concepts](Core-Concepts.md) | Understand the engine contract, request placement, role control and state. |
| [Configuration](Configuration.md) | Look up fleet fields, environment inputs and defaults. |
| [CLI reference](CLI-Reference.md) | Look up commands, options and exit behaviour. |
| [API and data reference](API-and-Data-Reference.md) | Integrate HTTP routes, state, metrics and journals. |

## Contributing

[Contributing](https://github.com/athrael-soju/Narwhal/blob/main/CONTRIBUTING.md) covers development setup, local tests and the pull request workflow.
