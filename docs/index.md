# Narwhal documentation

Narwhal manages adaptive, disaggregated inference across a role-free fleet. Deployment starts from a management workstation: Narwhal inspects the target hosts, derives their configuration, prepares the remote engines, and verifies routed inference over the private deployment path.

<div class="narwhal-hero">
  <img src="assets/social-preview.png" alt="Narwhal, adaptive disaggregated inference on a role-free fleet">
</div>

## Start with the task you need to complete

| Goal                                                                                        | Guide                                    |
| ------------------------------------------------------------------------------------------- | ---------------------------------------- |
| Bring up a new fleet and confirm that requests are routed correctly                         | [Deploy a fleet](Deploy.md)              |
| Profile the fleet, select measurement ranges, calibrate SLOs, and measure a target workload | [Measure a fleet](Measure.md)            |
| Export metrics to Prometheus and inspect the fleet in Grafana                               | [Set up observability](Observability.md) |
| Manage ingress, routers, engines, and software upgrades                                     | [Operate Narwhal](Operate.md)            |
| Diagnose overload, engine failures, or router failures from the first visible symptom       | [Troubleshoot a fleet](Troubleshoot.md)  |

For a new deployment, begin with [Deploy a fleet](Deploy.md). Once the fleet is serving routed inference, use [Measure a fleet](Measure.md) to characterize the workload and [Set up observability](Observability.md) to establish operational visibility.

## Understand and configure the system

Use these references when you need to understand Narwhal's runtime model, change fleet configuration, or integrate with its interfaces.

### System model

[Core concepts](Core-Concepts.md) describes the contracts that govern engines, request placement, role control, and fleet state.

### Fleet configuration

[Configuration](Configuration.md) defines fleet configuration fields, environment inputs, and their defaults.

### Command-line interface

[CLI reference](CLI-Reference.md) documents available commands and options, including command exit behaviour.

### HTTP interfaces

[HTTP API reference](HTTP-API.md) covers completion, inspection, and lifecycle endpoints.

### Runtime data and persisted artifacts

[Telemetry and artifact reference](Telemetry-and-Artifacts.md) documents journals, profiles, metrics, and persisted contract versions.

## Development

To build or modify Narwhal itself, see [Contributing](https://github.com/athrael-soju/Narwhal/blob/main/CONTRIBUTING.md) for development environment setup, the local test workflow, and the pull request process.