---
description: Monitor a Narwhal fleet with Prometheus and a provisioned Grafana dashboard.
---

# Setting up observability

`make observe` starts Prometheus, Grafana, and Grafana Image Renderer for the deployed fleet. Prometheus collects router and engine metrics and evaluates alert rules. Grafana serves the provisioned Narwhal Orchestrator dashboard.

## Monitoring tasks

<div class="grid cards" markdown>

-   [Starting and verifying monitoring](observability/01-Start-and-Verify.md)

    ---

    Configure the monitored deployment, start Prometheus and Grafana, and verify targets.

-   [Accessing dashboards and isolating listeners](observability/02-Access.md)

    ---

    Reach the dashboard from a workstation and isolate a second monitoring stack.

-   [Reading the dashboard](observability/05-Dashboard.md)

    ---

    Read each Narwhal Orchestrator panel and engine state.

-   [GPU telemetry, alerts, and recovery](observability/03-Telemetry-and-Recovery.md)

    ---

    Feed GPU telemetry, inspect alerts, troubleshoot monitoring, and retain captures.

-   [Monitoring a WSL2 development fleet](observability/04-WSL2.md)

    ---

    Connect, start, and check monitoring for a WSL2 development fleet.

</div>
