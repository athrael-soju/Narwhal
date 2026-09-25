# Set up observability

`make observe` builds scrape targets from the deployed fleet's engine IDs and metrics endpoints, then starts Prometheus `3.14.0` and Grafana `13.2.1`. Prometheus collects router and engine metrics and evaluates alerts. Grafana displays them in the provisioned **Narwhal Orchestrator** dashboard.

## Monitoring tasks

- [Start and verify monitoring](observability/01-Start-and-Verify.md)
- [Access dashboards and isolate listeners](observability/02-Access.md)
- [Inspect GPU telemetry, alerts, and recovery](observability/03-Telemetry-and-Recovery.md)
