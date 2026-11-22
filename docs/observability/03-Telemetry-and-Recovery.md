---
description: Inspect Narwhal alerts, troubleshoot the monitoring stack, add a GPU exporter, and retain captures.
---

# Alerts, troubleshooting and captures

## Alert evaluation

Prometheus evaluates the rules in `tools/observability/prometheus-alerts.yml` and publishes their state in the `ALERTS` series. Each rule carries `severity="page"` or `severity="warn"`. Route those labels through your own Alertmanager.

List the loaded rules on the router host:

```bash
curl -fsS http://127.0.0.1:9090/api/v1/rules | python3 -m json.tool
```

Rules select the router by `job="narwhal-router"` and each engine by `job="engines"` and `iid=<engine identity>`.

## GPU metrics

The monitoring stack scrapes the router, the engines, the fleet control service and the Narwhal tools. To chart GPU sensor metrics, run a GPU exporter that Prometheus can reach, add a scrape job for it to `tools/observability/prometheus.yml`, rerun `make observe`, and build the GPU dashboard in Grafana.

## Troubleshooting

| Symptom | Check or action |
| --- | --- |
| Browser connection fails | Check the SSH tunnel and the route to the router host |
| `make observe` reports an occupied listener | Stop the reported process or socket unit, or [move to isolated listeners](02-Access.md#isolating-a-second-monitoring-stack) |
| Startup reports a command deadline | Check Docker daemon health, registry reachability, and the [Compose service status](#inspecting-compose-services) |
| Startup reports a container exit or readiness deadline | Inspect the Prometheus and Grafana [Compose logs](#inspecting-compose-services) |
| Prometheus reports `config permission denied`, or Grafana returns dashboard 404 | Follow the [mount permission failure](#mount-permission-failures) steps |
| Router target fails | Check `NARWHAL_ROUTER_URL`, its route from the router host, and Prometheus `/targets` |
| An engine replica disappears from charts | Check its entry in the router's `/narwhal/targets` and its `iid` label |
| Grafana shows an older dashboard | Rerun `make observe` |
| `make observe` fails the [dashboard readiness check](01-Start-and-Verify.md#readiness-contract) | Read the Grafana provisioning log |
| An alert evaluates against the wrong scope | Inspect target relabelling for `job`, `instance`, and `iid` |

After a fix, rerun `make observe`. It returns once the [readiness contract](01-Start-and-Verify.md#readiness-contract) passes.

### Inspecting Compose services

Every Compose command for `tools/observability/compose.yml` requires `NARWHAL_RENDERER_TOKEN`, which `make observe` stores in `runs/observability/renderer-token`.

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

[Monitor placement and control](../operate/02-Monitor.md#monitoring-placement-and-control)
