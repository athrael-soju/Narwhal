# Set up observability

`make observe` starts two services for the deployed fleet:

| Service | Role |
| --- | --- |
| Prometheus `3.14.0` | Router and engine metrics, alert rules |
| Grafana `13.2.1` | Provisioned Narwhal Orchestrator dashboard |

## Monitoring tasks

- [Start and verify monitoring](observability/01-Start-and-Verify.md)
- [Access dashboards and isolate listeners](observability/02-Access.md)
- [GPU telemetry, alerts, and recovery](observability/03-Telemetry-and-Recovery.md)
- [Monitor a WSL2 development fleet](observability/04-WSL2.md)
