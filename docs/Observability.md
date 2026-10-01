---
description: Monitor a Narwhal fleet with Prometheus and a provisioned Grafana dashboard.
---

# Set up observability

`make observe` starts three services for the deployed fleet:

| Service | Role |
| --- | --- |
| Prometheus `3.14.0` | Router and engine metrics, alert rules |
| Grafana `13.2.1` | Provisioned Narwhal Orchestrator dashboard |
| Grafana Image Renderer `5.12.5` | PNG renders of dashboards and panels for Grafana |

## Monitoring tasks

<div class="grid cards" markdown>

-   [Start and verify monitoring](observability/01-Start-and-Verify.md)

    ---

    Configure the monitored deployment, start Prometheus and Grafana, and verify targets.

-   [Access dashboards and isolate listeners](observability/02-Access.md)

    ---

    Reach the dashboard from a workstation and isolate a second monitoring stack.

-   [Read the dashboard](observability/05-Dashboard.md)

    ---

    Read the headline blocks, engine states, fleet events, role history, outcomes, latency, and pool panels.

-   [GPU telemetry, alerts, and recovery](observability/03-Telemetry-and-Recovery.md)

    ---

    Feed GPU telemetry, inspect alerts, troubleshoot monitoring, and retain captures.

-   [Monitor a WSL2 development fleet](observability/04-WSL2.md)

    ---

    Connect, start, and check monitoring for a WSL2 development fleet.

</div>
