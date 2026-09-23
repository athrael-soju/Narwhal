# Set up observability

`make observe` builds scrape targets from the deployed fleet's engine IDs and metrics endpoints, then starts Prometheus `3.14.0` for router and engine metrics and alert evaluation and Grafana `13.2.1` with the provisioned **Narwhal Orchestrator** dashboard.

## Monitoring tasks

- [Start and verify monitoring](observability/01-Start-and-Verify.md)
- [Access dashboards and isolate listeners](observability/02-Access.md)
- [Inspect GPU telemetry, alerts, and recovery](observability/03-Telemetry-and-Recovery.md)
