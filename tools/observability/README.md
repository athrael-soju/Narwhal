# Observability asset contracts

The pinned Compose project starts Prometheus and Grafana with Narwhal alert rules and the provisioned **Narwhal Orchestrator** dashboard, plus the Grafana Image Renderer for PNG renders of dashboards and panels. Follow [Set up observability](../../docs/Observability.md) to select listeners, start and verify monitoring, access the dashboard, and recover failed components.

## Dashboard

[Read the dashboard](../../docs/observability/05-Dashboard.md) explains each panel and engine state.

**Narwhal Orchestrator** joins the selected router's metrics with engine scrapes by `iid`. Each block in the first row pairs a status value, coloured against its target, with a context value: goodput within the SLOs and its request count, the unserved share and the offered count, TTFT and TPOT p95 as a share of their SLOs and in seconds, out-of-service engines and flips, and router admission and firing Narwhal alerts. The engine table lists engines in ID order and shows resident Narwhal work, native vLLM work, KV occupancy, prefix cache hits and token rate. The role history beside it, at a third of the row, marks outage periods in the same colours as the table's State column. Request outcomes follow, with fleet events (a timeline of the Narwhal alerts that fired in the displayed interval) beside them at a quarter of the row, then pool assignments, TTFT and TPOT quantiles beside token throughput, and pool pressure, request-wait quantiles and retries side by side. Request outcomes also marks when each alert started firing.

The dashboard selects every router target in the data source when it opens. The shipped scrape configuration binds one router and one fleet to each data source, so the bare dashboard URL immediately populates router totals and pool pressure. **Engine detail** filters the engine table and role timeline.

The dashboard expects these label contracts:

- Router series carry `job="narwhal-router"` and the Prometheus target's `instance`.
- Engine series carry `job="engines"` and the fleet generator's stable `iid`.
- vLLM exposes `vllm:num_requests_running`, `vllm:num_requests_waiting` and
  `vllm:kv_cache_usage_perc` on each engine metrics endpoint.
- Prometheus exposes evaluated rules through the `ALERTS` series.

Run the deployment's selected AMD or NVIDIA exporter to discover GPUs and collect sensor telemetry, then present those metrics through its hardware dashboard.

### Metric boundaries

**Goodput** divides requests completed within both the TTFT and TPOT SLOs (`narwhal_slo_met_total`) by every request that reached an outcome when it ended, so refused, rejected, failed, expired and cancelled requests count as misses. **Load** reports the share of ended requests that were refused or rejected at admission or ended by failure or deadline expiry, with the offered count. Goodput, Load and the headline p95 values sum `increase()` over the displayed interval. **Request outcomes** plots each terminal outcome per second, and client cancellations have their own series. **Token throughput** plots router-observed output tokens and the prompt tokens engines prefilled per second, from vLLM's `local_compute` and `local_cache_hit` prompt token sources when the engine reports them. The flip count sums `increase()` over the displayed interval, so router counter resets preserve it.

**Time to first token** and **Time per output token** calculate p50, p95 and p99 from bucket rates grouped by `instance` and `le`. **Request waiting time** uses the same `instance` and `le` grouping to calculate queue-wait and seat-time p95. Each restart begins a fresh histogram. The dashboard selects one router before calculating quantiles, while `narwhal_slo_seconds` supplies that process's configured TTFT and TPOT lines. Aggregating latency buckets across routers requires identical SLO-derived bucket edges.

Each `iid` identifies one logical engine replica. Role changes affect new placements; resident requests remain assigned until completion. A router scrape failure withdraws current assignment and queue series. Engine latency covers engine processing, while deployment client samples establish end-to-end SLO attainment over offered requests.

[Telemetry and artifact reference](../../docs/telemetry/03-Metrics-and-Control.md#read-live-state-from-prometheus) defines the metric groups and lifecycle.

## Dashboard maintenance

`make observe` stages `tools/observability/grafana-narwhal.json` at `runs/observability/mounts/grafana-dashboards/narwhal.json`. Grafana polls that directory mount every 30 seconds and replaces UI edits from the staged file. The file uses the `dashboard.grafana.app/v2alpha1` schema, and startup reads it back through Grafana's `v2beta1` dashboard API. Refresh the staged copy and verify provisioning after changing the source dashboard:

```bash
make observe
curl -fsS http://127.0.0.1:3000/api/health
```

Validate changed queries against traffic, idle engines, failed scrapes, router restart and the **Engine detail** selector before deploying the dashboard. Keep live addresses and captured responses under `runs/`.

## Alert rules

Prometheus loads `tools/observability/prometheus-alerts.yml` and publishes firing rules through `ALERTS`, which drives the dashboard's **Fleet events** table. Production monitoring loads the same rule file and routes page and warning severities through the deployment's existing alert manager. Preserve the `job` and `iid` labels when relabelling targets because engine reachability and scoped alert rows depend on them.
