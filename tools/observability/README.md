# Observability asset contracts

The pinned Compose project supplies Prometheus, Grafana, Narwhal alert rules and the provisioned **Narwhal Orchestrator** dashboard. [Set up observability](../../docs/Observability.md) owns listener selection, startup, verification, access and recovery procedures.

## Dashboard

**Narwhal Orchestrator** joins the selected router's metrics with engine scrapes by `iid`. The first row reports admission, controller mode, engine reachability, terminal request rates, token rates and firing Narwhal alerts. The engine table shows current role, drain and ejection state, resident Narwhal work, native vLLM work and KV occupancy. Pool assignments and role history lead into TTFT, TPOT and request-wait quantiles, followed by request flow, exceptions and pool pressure.

The dashboard selects every router target in the data source when it opens. The shipped scrape configuration binds one router and one fleet to each data source, so the bare dashboard URL immediately populates router totals and pool pressure. **Engine detail** filters the engine table and role timeline.

The dashboard expects these label contracts:

- Router series carry `job="narwhal-router"` and the Prometheus target's `instance`.
- Engine series carry `job="engines"` and the fleet generator's stable `iid`.
- vLLM exposes `vllm:num_requests_running`, `vllm:num_requests_waiting` and
  `vllm:kv_cache_usage_perc` on each engine metrics endpoint.
- Prometheus exposes evaluated rules through the `ALERTS` series.

Run the deployment's selected AMD or NVIDIA exporter to discover GPUs and collect sensor telemetry, then present those metrics through its hardware dashboard.

### Metric boundaries

**Requests** counts completed, failed, expired, refused and rejected terminal outcomes per second, while client cancellations have their own counter. **Tokens** counts engine prompt tokens as prefill and router-observed output tokens as decode. Each panel sums `increase()` over the displayed interval, so router and engine counter resets preserve the interval count.

**Time to first token** and **Time per output token** calculate p50, p95 and p99 from bucket rates grouped by `instance` and `le`. **Request waiting time** applies the same boundary to queue-wait and seat-time p95. Each restart begins a fresh histogram. The dashboard selects one router before calculating quantiles, while `narwhal_slo_seconds` supplies that process's configured TTFT and TPOT lines. Aggregating latency buckets across routers requires identical SLO-derived bucket edges.

Each `iid` identifies one logical engine replica. Role changes affect new placements; resident requests remain assigned until completion. A router scrape failure withdraws current assignment and queue series. Engine latency covers engine processing, while deployment client samples establish end-to-end SLO attainment over offered requests.

[Telemetry and artifact reference](../../docs/Telemetry-and-Artifacts.md#metrics) defines the metric groups and lifecycle.

## Dashboard maintenance

`make observe` stages `tools/observability/grafana-narwhal.json` at `runs/observability/mounts/grafana-dashboards/narwhal.json`. Grafana polls that directory mount every 30 seconds and replaces UI edits from the staged file. Refresh the staged copy and verify provisioning after changing the source dashboard:

```bash
make observe
curl -fsS http://127.0.0.1:3000/api/health
```

Validate changed queries against traffic, idle engines, failed scrapes, router restart and the **Engine detail** selector before deploying the dashboard. Keep live addresses and captured responses under `runs/`.

## Alert rules

Prometheus loads `tools/observability/prometheus-alerts.yml` and publishes firing rules through `ALERTS`, which drives the dashboard's **Fleet events** table. Production monitoring loads the same rule file and routes page and warning severities through the deployment's existing alert manager. Preserve the `job` and `iid` labels when relabelling targets because engine reachability and scoped alert rows depend on them.
