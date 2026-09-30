# GPU telemetry, alerts, and troubleshooting

## GPU telemetry

`make observe` scrapes the router and the engines and ships no hardware dashboard. For GPU metrics, run the AMD or NVIDIA exporter for your hardware and view the data in that exporter's dashboard.

## Alerts

Prometheus loads its alert rules from `tools/observability/prometheus-alerts.yml`. To see which rules loaded and how they're evaluating:

```bash
curl -fsS http://127.0.0.1:9090/api/v1/rules | python3 -m json.tool
```

Pending and firing alerts also appear as the `ALERTS` metric.

The rules pick their targets by label. Router scrapes have `job="narwhal-router"`. Engine scrapes have `job="engines"` plus an `iid` label holding the engine's identity. The `job` labels come from the scrape jobs in `prometheus.yml`. The generated targets set `iid`.

The stack that `make observe` starts sends alerts nowhere. Production monitoring loads the same rules file. The deployment's Alertmanager routes alerts labeled `severity="page"` or `severity="warn"`.

## Troubleshooting

| Problem                                       | What to do                                                                                                                                                                                                                                                                            |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| The browser can't connect                     | Check that the SSH tunnel is running and forwards the local port, and that the router host is reachable.                                                                                                                                                                             |
| `make observe` says a listener is in use      | Stop the process or socket unit it names, or [run this stack on its own address](02-Access.md#run-a-second-monitoring-stack-on-the-same-host).                                                                                                                                        |
| A command times out during startup            | Check the Docker daemon and registry reachability, then run `docker compose -f tools/observability/compose.yml ps`.                                                                                                                                           |
| A container exits, or readiness times out     | Read `docker compose -f tools/observability/compose.yml logs prometheus grafana` and confirm the containers are running the pinned image versions.                                                                                                                                    |
| Prometheus reports `config permission denied` | The Compose file and the startup script probably come from different versions. Rerun `make observe` from the deployed checkout so they match. If the error persists, check the [config file permissions](01-Start-and-Verify.md#where-the-config-files-live) and the container logs. |
| Grafana returns 404 for the dashboard         | Usually the same cause and fix as the permission error above. If a rerun fails to fix it, check Grafana's logs.                                                                                                                                                               |
| The router target is down                     | Check `NARWHAL_ROUTER_URL` and that Prometheus can reach it from the router host. The `/targets` page shows the scrape error.                                                                                                                                                         |
| An engine drops off the charts                | Check its entry in the generated targets, whether its metrics endpoint is reachable, and its `iid` label.                                                                                                                                                                             |
| Grafana shows an old version of the dashboard | Rerun `make observe`. Grafana reloads the file within 30 seconds. If the [readiness checks](01-Start-and-Verify.md#what-ready-means) still fail, read Grafana's provisioning log.                                                                                  |
| An alert fires for the wrong target           | Check the `job`, `instance`, and `iid` labels on the targets.                                                                                                                                                                                                                         |

## Keep a record

Save deployment addresses and captured monitoring output, including failures, under `runs/`. Add the verified Prometheus targets and dashboard queries to the load record from [deployment acceptance](../deploy/07-Serve-and-Measure.md).

If the fleet itself is unhealthy, see [Monitor placement and control](../operate/02-Monitor.md#6-monitor-placement-and-control).
