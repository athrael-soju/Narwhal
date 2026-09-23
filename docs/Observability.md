# Set up observability

Narwhal ships a Compose-based monitoring stack for the router host. It runs:

- Prometheus `3.14.0` for router and engine scraping, alert evaluation, and dashboard data.
- Grafana `13.2.1` with a provisioned **Narwhal Orchestrator** dashboard.

The monitoring stack is generated from the deployed fleet definition, so Prometheus target identities and metrics endpoints remain aligned with the running deployment.

## Monitoring tasks

- [Start and verify monitoring](observability/01-Start-and-Verify.md)
- [Access dashboards and isolate listeners](observability/02-Access.md)
- [Inspect GPU telemetry, alerts, and recovery](observability/03-Telemetry-and-Recovery.md)
