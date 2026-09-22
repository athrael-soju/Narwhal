# Narwhal documentation

<div class="narwhal-hero">
  <img src="assets/social-preview.png" alt="Narwhal, adaptive disaggregated inference on a role-free fleet">
  <p class="narwhal-hero__copy"><a href="Deploy.md">Deploy a fleet</a> from a management workstation. Narwhal inspects the hosts to derive configuration, prepares the remote engines, and checks routed inference over the private deployment path.</p>
</div>

## Deployment and operations

| Guide                                    | Covers                                                                               |
| ---------------------------------------- | ------------------------------------------------------------------------------------ |
| [Deploy a fleet](Deploy.md)              | Provision engines and the router, then verify routed inference.                      |
| [Measure a fleet](Measure.md)            | Choose profiling ranges, calibrate SLOs, and measure the target workload.            |
| [Set up observability](Observability.md) | Configure Prometheus targets and use the Grafana dashboard.                          |
| [Operate Narwhal](Operate.md)            | Manage ingress, routers, engines, and upgrades.                                      |
| [Troubleshoot a fleet](Troubleshoot.md)  | Trace overload, engine failures, and router failures from the first observed signal. |

## Architecture and reference

| Reference                                                      | Covers                                                              |
| -------------------------------------------------------------- | ------------------------------------------------------------------- |
| [Core concepts](Core-Concepts.md)                              | Engine contracts, request placement, role control, and fleet state. |
| [Configuration](Configuration.md)                              | Fleet configuration fields, environment inputs, and defaults.       |
| [CLI reference](CLI-Reference.md)                              | Commands, options, and exit behaviour.                              |
| [HTTP API reference](HTTP-API.md)                              | Completion, inspection, and lifecycle endpoints.                    |
| [Telemetry and artifact reference](Telemetry-and-Artifacts.md) | Journals, profiles, metrics, and persisted contract versions.       |

## Contributing

[Contributing](https://github.com/athrael-soju/Narwhal/blob/main/CONTRIBUTING.md) documents the development environment, local test workflow, and pull request process.