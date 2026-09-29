# GPU telemetry, alerts, and recovery

## GPU telemetry

Run the AMD or NVIDIA exporter. It finds the GPUs and collects sensor metrics, and its hardware dashboard displays them.

## Alert evaluation

Prometheus evaluates `tools/observability/prometheus-alerts.yml`. Its alert state appears in the `ALERTS` series.

Inspect rules on the router host:

```bash
curl -fsS http://127.0.0.1:9090/api/v1/rules | python3 -m json.tool
```

| Target | Labels |
| --- | --- |
| Router | `job="narwhal-router"` |
| Engine | `job="engines"`, `iid=<engine identity>` |

Alert rules select targets by these labels. Production uses the same rules file. The deployment's alert manager routes `severity="page"` and `severity="warn"` alerts.

## Troubleshooting

| Symptom | Check or action |
| --- | --- |
| Browser connection fails | Check the SSH tunnel and the route to the router host. |
| `make observe` reports an occupied listener | Stop the reported process or socket unit, or [move to isolated listeners](02-Access.md#isolate-a-second-monitoring-stack) with `NARWHAL_GRAFANA_BIND_ADDRESS` and `NARWHAL_PROMETHEUS_LISTEN_ADDRESS`. |
| Startup reports a command deadline | Check Docker daemon health, registry reachability, and `docker compose -f tools/observability/compose.yml ps`. |
| Startup reports a container exit or readiness deadline | Inspect `docker compose -f tools/observability/compose.yml logs prometheus grafana` and confirm the pinned image versions. |
| Prometheus reports `config permission denied` | See [Mount permission failures](#mount-permission-failures). |
| Grafana returns dashboard 404 | See [Mount permission failures](#mount-permission-failures). |
| Router target fails | Check `NARWHAL_ROUTER_URL`, its route from the router host, and Prometheus `/targets`. |
| An engine replica disappears from charts | Check the generated target entry and the engine's `iid` label. |
| Grafana shows an older dashboard | Rerun `make observe` to replace the staged dashboard. If the [dashboard readiness checks](01-Start-and-Verify.md#readiness-contract) still fail, read the provisioning log. |
| An alert evaluates against the wrong scope | Inspect target relabelling for `job`, `instance`, and `iid`. |

Rerun `make observe`. Success means the [readiness contract](01-Start-and-Verify.md#readiness-contract) passes.

### Mount permission failures

1. Confirm `tools/observability/start.py` and `tools/observability/compose.yml` share one checkout.
2. Rerun `make observe` from that deployed checkout.
3. Read the [staged mount permissions](01-Start-and-Verify.md#staged-monitoring-files) and container logs.
4. Save the failure output in the private deployment record.

## Retain monitoring captures

Save deployment addresses and captured responses under `runs/`. Attach the verified Prometheus targets and dashboard queries to the load record from the [deployment acceptance sequence](../deploy/07-Serve-and-Measure.md).

For fleet-health actions, follow [Monitor placement and control](../operate/02-Monitor.md#6-monitor-placement-and-control).
