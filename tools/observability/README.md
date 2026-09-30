# Observability assets

Reference for the Grafana dashboard and Prometheus alert rules in this directory. For setup and troubleshooting, see [Set up observability](../../docs/Observability.md).

## Dashboard

The provisioned **Narwhal Orchestrator** dashboard joins the selected router's metrics with engine scrapes by `iid`. Rows, top to bottom:

- Summary: admission, controller mode, engine reachability, terminal request rates, token rates, and firing Narwhal alerts.
- Engine table: current role, drain and ejection state, resident Narwhal work, native vLLM work, and KV cache occupancy.
- Pool assignments and role history.
- Time to first token (TTFT), time per output token (TPOT), and request waiting time quantiles.
- Request flow, exceptions, and pool pressure.

On open, the dashboard selects every router target in the data source. The shipped scrape configuration binds one router and one fleet to each data source, so the dashboard shows router totals and pool pressure with no selector change. **Engine detail** filters the engine table and role timeline.

The dashboard expects these labels and metrics:

- Router series carry `job="narwhal-router"` and the Prometheus target's `instance`.
- Engine series carry `job="engines"` and the target generator's stable `iid`.
- vLLM exposes `vllm:num_requests_running`, `vllm:num_requests_waiting`, `vllm:kv_cache_usage_perc`, and `vllm:prompt_tokens_total` on each engine metrics endpoint.
- Prometheus exposes pending and firing alerts through the `ALERTS` series.

Run the deployment's selected AMD or NVIDIA exporter to discover GPUs and collect sensor telemetry. Its hardware dashboard presents those metrics.

### Metric boundaries

- **Requests**: per-second rate of terminal outcomes (completed, failed, expired, refused, rejected). Client cancellations use a separate counter.
- **Tokens**: engine prompt tokens count as prefill. Router-observed output tokens count as decode.
- **Requests** and **Tokens** sum `increase()` over the displayed interval, which absorbs counter resets in the router or engine.
- **Time to first token** and **Time per output token**: p50, p95, and p99 from bucket rates grouped by `instance` and `le`.
- **Request waiting time**: queue-wait and seat-time p95, grouped the same way.
- A restart starts a fresh histogram.
- Quantiles are computed for one selected router. `narwhal_slo_seconds` draws that router's configured TTFT and TPOT threshold lines.
- Aggregating latency buckets across routers requires identical SLO-derived bucket edges.
- Each `iid` identifies one logical engine replica.
- Role changes affect new placements. Resident requests stay assigned until they complete.
- A router scrape failure withdraws the current assignment and queue series.
- Engine latency covers engine processing. End-to-end SLO attainment comes from client-side samples of all offered requests.

See [Metrics and control](../../docs/telemetry/03-Metrics-and-Control.md#read-live-state-from-prometheus) for the metric groups.

## Dashboard maintenance

`make observe` stages `tools/observability/grafana-narwhal.json` at `runs/observability/mounts/grafana-dashboards/narwhal.json`. Grafana polls that directory mount every 30 seconds and replaces UI edits with the staged file. After changing the source dashboard, run `make observe` to restage it and confirm Grafana is healthy:

```bash
make observe
curl -fsS http://127.0.0.1:3000/api/health
```

Before deploying a changed query, test it against:

- loaded traffic
- idle engines
- a failed scrape
- a router restart
- the **Engine detail** selector

Keep live addresses and captured responses under `runs/`.

## Alert rules

Prometheus loads `tools/observability/prometheus-alerts.yml`. Production monitoring loads the same rule file and routes the `page` and `warn` severities through the deployment's existing alert manager. Firing rules are published through `ALERTS`, which drives the dashboard's **Fleet events** table. Keep the `job` and `iid` labels when relabeling targets. Engine reachability and scoped alert rows depend on them.
