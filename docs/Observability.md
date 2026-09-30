# Set up observability

`make observe` starts two services for the deployed fleet:

| Service | Role |
| --- | --- |
| Prometheus `3.14.0` | Scrapes the router and engine metrics and evaluates the alert rules |
| Grafana `13.2.1` | Displays the results in the provisioned Narwhal Orchestrator dashboard |

## Monitoring tasks

- [Start and verify monitoring](observability/01-Start-and-Verify.md): fleet and router variables, `make observe`, and scrape target checks.
- [Access dashboards and isolate listeners](observability/02-Access.md): workstation tunnels to Grafana and Prometheus, and a second stack on the router host with distinct loopback listeners.
- [GPU telemetry, alerts, and recovery](observability/03-Telemetry-and-Recovery.md): GPU sensor metrics, alert rules, and monitoring troubleshooting.
- [Monitor a WSL2 development fleet](observability/04-WSL2.md): a `narwhal dev` fleet in WSL2, scraped from a separate monitoring host.
