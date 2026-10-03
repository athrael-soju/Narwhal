---
description: Feed AMD or NVIDIA GPU telemetry to the exporter's hardware dashboard, inspect Narwhal alerts, troubleshoot monitoring, and retain captures.
---

# GPU telemetry, alerts, and recovery

## GPU telemetry

Run the deployment's AMD or NVIDIA exporter to feed GPU sensor metrics to its hardware dashboard.

## Alert evaluation

Prometheus publishes the state of the `tools/observability/prometheus-alerts.yml` rules in the `ALERTS` series.

Inspect rules on the router host:

```bash
curl -fsS http://127.0.0.1:9090/api/v1/rules | python3 -m json.tool
```

Alert rules select the router by `job="narwhal-router"` and each engine by `job="engines"` and `iid=<engine identity>`.

The deployment's alert manager routes `severity="page"` and `severity="warn"` alerts in production.

## Troubleshooting

Find the symptom and apply the check or action beside it:

| Symptom | Check or action |
| --- | --- |
| Browser connection fails | Check the SSH tunnel and the route to the router host |
| `make observe` reports an occupied listener | Stop the reported process or socket unit, or [move to isolated listeners](02-Access.md#isolating-a-second-monitoring-stack) |
| Startup reports a command deadline | Check Docker daemon health, registry reachability, and the [Compose service status](#inspecting-compose-services) |
| Startup reports a container exit or readiness deadline | Inspect the Prometheus and Grafana [Compose logs](#inspecting-compose-services) |
| Prometheus reports `config permission denied` | Follow the [mount permission failure](#mount-permission-failures) steps |
| Grafana returns dashboard 404 | Follow the [mount permission failure](#mount-permission-failures) steps |
| Router target fails | Check `NARWHAL_ROUTER_URL`, its route from the router host, and Prometheus `/targets` |
| An engine replica disappears from charts | Check the generated target entry and the engine's `iid` label |
| Grafana shows an older dashboard | Rerun `make observe` |
| `make observe` fails the [dashboard readiness check](01-Start-and-Verify.md#readiness-contract) | Read the Grafana provisioning log |
| An alert evaluates against the wrong scope | Inspect target relabelling for `job`, `instance`, and `iid` |

Verify each fix with a `make observe` rerun that passes the [readiness contract](01-Start-and-Verify.md#readiness-contract).

### Inspecting Compose services

Every Compose command for `tools/observability/compose.yml` requires `NARWHAL_RENDERER_TOKEN`.

`make observe` stores the token in `runs/observability/renderer-token`.

Load the token in the deployed checkout on the router host, then query the services:

```bash
export NARWHAL_RENDERER_TOKEN="$(cat runs/observability/renderer-token)"
docker compose -f tools/observability/compose.yml ps
docker compose -f tools/observability/compose.yml logs prometheus grafana
```

### Mount permission failures

1. Confirm `tools/observability/start/` and `tools/observability/compose.yml` share one checkout.
2. Rerun `make observe` from that deployed checkout.
3. Read the [staged mount permissions](01-Start-and-Verify.md#staged-monitoring-files) and container logs.
4. Save the failure output in the private deployment record.

## Retaining monitoring captures

- Save deployment addresses and captured responses under `runs/`.
- Attach the verified Prometheus targets and dashboard queries to the load record from the [deployment acceptance sequence](../deploy/07-Serve-and-Measure.md).

[![Next: Monitoring placement and control](https://img.shields.io/badge/next-Monitoring%20placement%20and%20control-0f766e)](../operate/02-Monitor.md#6-monitoring-placement-and-control)
