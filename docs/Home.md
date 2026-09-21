<p align="center">
  <img src="../assets/social-preview.png" alt="Narwhal logo and wordmark" width="100%">
</p>

# Narwhal documentation

Narwhal profiles the fleet's engines, admits each request against measured SLO budgets, assigns prefill and decode to separate engines, and shifts the live split as demand changes while model weights stay resident.

Start with [Deploy a fleet](03-Deploy.md) from your management workstation and fresh checkout. Use the supplied private management access and inventory to reach the designated router and engine hosts, install there, and check GPUs on the engine hosts.

| Page | Use it for |
| --- | --- |
| [Deploy a fleet](03-Deploy.md) | Start here with supplied fleet access and verify one engine host before launch. |
| [Core concepts](02-Core-Concepts.md) | Understand engines, request placement, reactive control, and state. |
| [Operate Narwhal](04-Operate.md) | Run ingress, telemetry, failover, and engine maintenance. |
| [Troubleshoot a fleet](05-Troubleshoot.md) | Respond to overload, engine faults, and router faults. |
| [Measure a fleet](06-Measure.md) | Calibrate SLOs and validate the deployment under load. |
| [Configuration](07-Configuration.md) | Look up fleet fields and defaults. |
| [CLI reference](08-CLI-Reference.md) | Look up commands, options, and exit behaviour. |
| [API and data reference](09-API-and-Data-Reference.md) | Integrate HTTP routes, state, metrics, and journals. |
| [Observability](10-Observability.md) | Generate targets, verify Prometheus scrapes, and inspect the Grafana dashboard. |

## Development

[Contributing](https://github.com/athrael-soju/Narwhal/blob/main/CONTRIBUTING.md) covers development setup and checks.
