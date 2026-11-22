---
description: Monitor a Narwhal fleet with Prometheus and a provisioned Grafana dashboard.
---

# Setting up observability

`make observe` starts Prometheus, Grafana, and Grafana Image Renderer for the deployed fleet. Prometheus collects router and engine metrics and evaluates alert rules. Grafana serves the provisioned Narwhal Orchestrator dashboard.

- [Starting and verifying monitoring](observability/01-Start-and-Verify.md): configure the monitored deployment, start the stack, and verify targets.
- [Accessing dashboards and isolating listeners](observability/02-Access.md): reach the dashboard from a workstation and isolate a second monitoring stack.
- [Alerts, troubleshooting and captures](observability/03-Telemetry-and-Recovery.md): inspect alerts, troubleshoot monitoring, add a GPU exporter, and retain captures.
- [Reading the dashboard](observability/05-Dashboard.md): read each panel and engine state.
- [Monitoring a WSL2 development fleet](observability/04-WSL2.md): connect, start, and check monitoring for a development fleet.
