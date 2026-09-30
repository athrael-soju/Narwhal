# GPU telemetry, alerts, and recovery

## GPU telemetry

Run the deployment's AMD or NVIDIA exporter to feed GPU sensor metrics to the hardware dashboard.

## Alert evaluation

Prometheus publishes the state of the `tools/observability/prometheus-alerts.yml` rules in the `ALERTS` series.

Inspect rules on the router host:

```bash
curl -fsS http://127.0.0.1:9090/api/v1/rules | python3 -m json.tool
```

Alert rules select targets by these labels:

| Target | Labels |
| --- | --- |
| Router | `job="narwhal-router"` |
| Engine | `job="engines"`, `iid=<engine identity>` |

The deployment's alert manager routes `severity="page"` and `severity="warn"` alerts in production.

## Troubleshooting

| Symptom | Check or action |
| --- | --- |
| Browser connection fails | Check the SSH tunnel and the route to the router host. |
| `make observe` reports an occupied listener | Stop the reported process or socket unit, or [move to isolated listeners](02-Access.md#isolate-a-second-monitoring-stack). |
| Startup reports a command deadline | Check Docker daemon health, registry reachability, and `docker compose -f tools/observability/compose.yml ps`. |
| Startup reports a container exit or readiness deadline | Inspect `docker compose -f tools/observability/compose.yml logs prometheus grafana`. |
| Prometheus reports `config permission denied` | Follow the [mount permission failure](#mount-permission-failures) steps. |
| Grafana returns dashboard 404 | Follow the [mount permission failure](#mount-permission-failures) steps. |
| Router target fails | Check `NARWHAL_ROUTER_URL`, its route from the router host, and Prometheus `/targets`. |
| An engine replica disappears from charts | Check the generated target entry and the engine's `iid` label. |
| Grafana shows an older dashboard | Rerun `make observe`. |
| `make observe` fails the [dashboard readiness check](01-Start-and-Verify.md#readiness-contract) | Read the Grafana provisioning log. |
| An alert evaluates against the wrong scope | Inspect target relabelling for `job`, `instance`, and `iid`. |

Verify each fix with a `make observe` rerun that passes the [readiness contract](01-Start-and-Verify.md#readiness-contract).

### Mount permission failures

1. Confirm `tools/observability/start.py` and `tools/observability/compose.yml` share one checkout.
2. Rerun `make observe` from that deployed checkout.
3. Read the [staged mount permissions](01-Start-and-Verify.md#staged-monitoring-files) and container logs.
4. Save the failure output in the private deployment record.

## Retain monitoring captures

- Save deployment addresses and captured responses under `runs/`.
- Attach the verified Prometheus targets and dashboard queries to the load record from the [deployment acceptance sequence](../deploy/07-Serve-and-Measure.md).

[![Next: Monitor placement and control](https://img.shields.io/badge/next-Monitor%20placement%20and%20control-0f766e)](../operate/02-Monitor.md#6-monitor-placement-and-control)
