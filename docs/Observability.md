# Set up observability

`make observe` reads your deployed fleet, sets up scraping for the router and every engine, and starts Prometheus 3.14.0 and Grafana 13.2.1. Prometheus collects the metrics and evaluates alerts. Grafana shows them in the **Narwhal Orchestrator** dashboard.

- [Start and verify monitoring](observability/01-Start-and-Verify.md)
- [Open the dashboards](observability/02-Access.md)
- [GPU telemetry, alerts, and troubleshooting](observability/03-Telemetry-and-Recovery.md)
- [Monitor a WSL2 development fleet](observability/04-WSL2.md)
