# Observability asset contracts

The pinned Compose project starts Prometheus with the Narwhal alert rules, Grafana with the provisioned **Narwhal Orchestrator** dashboard, and the Grafana Image Renderer.

Follow [Setting up observability](../../docs/Observability.md) to select listeners, start and verify monitoring, access the dashboard, and recover failed components.

## Dashboard

<a href="../../docs/observability/05-Dashboard.md"><img src="https://img.shields.io/badge/docs-Reading%20the%20dashboard-0f766e" alt="Reading the dashboard documentation"></a>

**Narwhal Orchestrator** joins the selected router's metrics with engine scrapes by `iid`.

The shipped scrape configuration binds one router and one fleet to each Prometheus data source.

The dashboard expects these label contracts:

- Router series carry `job="narwhal-router"` and the Prometheus target's `instance`.
- Engine series carry `job="engines"` and the fleet generator's stable `iid`.
- vLLM exposes `vllm:num_requests_running`, `vllm:num_requests_waiting` and
  `vllm:kv_cache_usage_perc` on each engine metrics endpoint.
- Prometheus exposes evaluated rules through the `ALERTS` series.

Run the deployment's selected AMD or NVIDIA exporter to discover GPUs and collect sensor telemetry, then present those metrics through its hardware dashboard.

### Metric boundaries

<table width="100%" align="center">
  <thead>
    <tr>
      <th align="left" width="25%">Value or series</th>
      <th align="left" width="75%">Measures</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td><b>Goodput</b></td>
      <td><code>narwhal_slo_met_total</code> over completed, failed, refused, rejected, expired and cancelled requests</td>
    </tr>
    <tr>
      <td><b>Load</b></td>
      <td>Refused, rejected, failed and expired requests over the <b>Goodput</b> denominator</td>
    </tr>
    <tr>
      <td><b>Request outcomes</b></td>
      <td>Offered requests and each terminal outcome per second</td>
    </tr>
    <tr>
      <td><b>Prefill/s</b></td>
      <td>Engine prompt tokens per second from vLLM's <code>local_compute</code> and <code>local_cache_hit</code> sources, otherwise <code>vllm:prompt_tokens_total</code></td>
    </tr>
    <tr>
      <td><b>Decode/s</b></td>
      <td>Router-observed output tokens per second</td>
    </tr>
  </tbody>
</table>

Goodput, Load, the headline p95 values and the flip count sum `increase()` over the displayed interval.

**Time to first token** and **Time per output token** calculate p50, p95 and p99 from bucket rates grouped by `instance` and `le`. **Request waiting time** uses the same `instance` and `le` grouping to calculate queue-wait and seat-time p95. Each restart begins a fresh histogram. The dashboard selects one router before calculating quantiles, while `narwhal_slo_seconds` supplies that process's configured TTFT and TPOT lines. Aggregating latency buckets across routers requires identical SLO-derived bucket edges.

Each `iid` identifies one logical engine replica. Role changes affect new placements; resident requests remain assigned until completion. A router scrape failure withdraws current assignment and queue series. Engine latency covers engine processing, while deployment client samples establish end-to-end SLO attainment over offered requests.

[Telemetry and artifact reference](../../docs/telemetry/03-Metrics-and-Control.md#reading-live-state-from-prometheus) defines the metric groups and lifecycle.

## Dashboard maintenance

`make observe` stages the dashboard for Grafana:

<table width="100%" align="center">
  <thead>
    <tr>
      <th align="left" width="30%">Item</th>
      <th align="left" width="70%">Value</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>Source dashboard</td>
      <td><code>tools/observability/grafana-narwhal.json</code></td>
    </tr>
    <tr>
      <td>Schema</td>
      <td><code>dashboard.grafana.app/v2alpha1</code></td>
    </tr>
    <tr>
      <td>Staged copy</td>
      <td><code>runs/observability/mounts/grafana-dashboards/narwhal.json</code></td>
    </tr>
    <tr>
      <td>Grafana poll interval</td>
      <td>30 seconds</td>
    </tr>
  </tbody>
</table>

Grafana replaces UI edits with the staged file.

Refresh the staged copy and verify provisioning after changing the source dashboard:

```bash
make observe
curl -fsS http://127.0.0.1:3000/api/health
```

Validate changed queries against traffic, idle engines, failed scrapes, router restart and the **Engine detail** selector before deploying the dashboard. Keep live addresses and captured responses under `runs/`.

## Alert rules

Alert rules in `tools/observability/prometheus-alerts.yml`:

- Prometheus publishes firing rules through `ALERTS` to the dashboard's **Fleet events** timeline.
- Production monitoring loads the same rule file and routes page and warning severities through the deployment's alert manager.
- Target relabelling keeps the `job` and `iid` labels for engine reachability and scoped alert rows.
