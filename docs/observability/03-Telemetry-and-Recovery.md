# GPU telemetry, alerts, and recovery

## GPU telemetry

Run the AMD or NVIDIA exporter selected for the deployment to:

- discover GPU devices;
- collect sensor metrics;
- publish those metrics through the deployment's hardware dashboard.

## Alert evaluation

Prometheus loads:

```text
tools/observability/prometheus-alerts.yml
```

Evaluated and firing alerts are exposed through the `ALERTS` series.

Inspect the loaded and evaluated rules with:

```bash
curl -fsS http://127.0.0.1:9090/api/v1/rules | python3 -m json.tool
```

The rules depend on the labels generated for the monitoring targets:

```text
job="narwhal-router"
job="engines"
iid=<engine identity>
```

The target generator supplies these labels.

Production monitoring uses the same rules file and routes:

- `severity="page"`
- `severity="warn"`

through the deployment's alert manager.

## Troubleshooting

| Failure                                                | What to inspect                                                                                                                                                                                                                                                            |
| ------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Browser cannot connect                                 | Confirm the SSH tunnel is running, verify the local forwarded port, and check the route to the router host.                                                                                                                                                                |
| `make observe` reports an occupied listener            | Stop the reported process or socket unit, or move the deployment to isolated listeners with `NARWHAL_GRAFANA_BIND_ADDRESS` and `NARWHAL_PROMETHEUS_LISTEN_ADDRESS`.                                                                                                        |
| Startup reports a command deadline                     | Check Docker daemon health, registry reachability, and `docker compose -f tools/observability/compose.yml ps`.                                                                                                                                                             |
| Startup reports a container exit or readiness deadline | Inspect `docker compose -f tools/observability/compose.yml logs prometheus grafana` and confirm that the pinned image versions are being used.                                                                                                                             |
| Prometheus reports `config permission denied`          | Use the updated monitoring startup helper and Compose file together, then rerun `make observe` from the same deployed checkout. If readiness still fails, inspect staged mount permissions and container logs. Retain the failure output in the private deployment record. |
| Grafana returns dashboard 404                          | Use the updated monitoring startup helper and Compose file together, rerun `make observe`, then inspect staged mount permissions and Grafana logs if readiness still fails. Retain the failure output in the private deployment record.                                    |
| Router target fails                                    | Check `NARWHAL_ROUTER_URL`, verify that Prometheus can route to it from the router host, and inspect Prometheus `/targets`.                                                                                                                                                |
| An engine replica disappears from charts               | Check the generated target entry, reachability of that engine's metrics endpoint, and its `iid` label.                                                                                                                                                                     |
| Grafana shows an older dashboard                       | Rerun `make observe` to replace the staged dashboard. The directory mount exposes the replacement to Grafana's provisioner. If the dashboard contract still fails, inspect the provisioning log.                                                                           |
| An alert evaluates against the wrong scope             | Inspect target relabelling for `job`, `instance`, and `iid`.                                                                                                                                                                                                               |

## Retained deployment evidence

Store deployment addresses and captured monitoring responses under:

```text
runs/
```

[Operate Narwhal](../operate/02-Monitor.md#6-monitor-placement-and-control) defines fleet-health semantics and the corresponding operator actions.

The [deployment acceptance sequence](../deploy/07-Serve-and-Measure.md) ties the verified Prometheus targets and dashboard queries to the retained load evidence.
