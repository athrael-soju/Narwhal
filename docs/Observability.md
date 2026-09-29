# Set up observability

`make observe` builds scrape targets from the deployed fleet's engine IDs and metrics endpoints, then starts Prometheus `3.14.0` and Grafana `13.2.1`. Prometheus scrapes the router and engine metrics and evaluates the alert rules. Grafana displays the results in the provisioned **Narwhal Orchestrator** dashboard.

## Monitoring tasks

- [Start and verify monitoring](observability/01-Start-and-Verify.md): set the fleet and router variables, run `make observe`, and check the scrape targets.
- [Access dashboards and isolate listeners](observability/02-Access.md): tunnel Grafana and Prometheus to a workstation, and run a second stack on the router host with distinct loopback listeners.
- [GPU telemetry, alerts, and recovery](observability/03-Telemetry-and-Recovery.md): inspect GPU sensor metrics and alert rules, and troubleshoot monitoring failures.
- [Monitor a WSL2 development fleet](observability/04-WSL2.md): scrape a `narwhal dev` fleet in WSL2 from a separate monitoring host.
