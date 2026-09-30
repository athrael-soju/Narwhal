# Router state and placement monitoring

## 5. Interpret router state

`/health` reports the state of the router process and the fleet behind it. `/ready` tells the load balancer whether to send traffic to this router.

### `/health`

`/health` returns HTTP 200 in every fleet state, including when all engines are down. The body has two fleet counts: `instances` is the configured fleet size, and `available_instances` is the number of engines eligible for placement, excluding ejected, draining, and quarantined engines.

### Backend exhaustion

When every engine is excluded from placement, the active router's `/health` reports `status: degraded` and `/ready` returns HTTP 503 with `reason: no available engines`. New completion requests fail with `backend_unavailable`. Clients can retry this error.

### Whole-wave lifecycle hold

During a whole-wave lifecycle hold, `/health` reports `status: maintenance`, `/ready` returns HTTP 503 with the hold as its `reason`, and completion requests get HTTP 503 with the error code `standby`. Background monitoring stops until the wave is readmitted.

### Temporary holds

Performance-drift and temporary-quarantine holds never exclude the last eligible engine. A failed health or inference probe can still exclude it.

### Router control and replacement

The active router keeps control during an engine outage or a managed maintenance window. Client load balancers route on HTTP status.

The standby keeps copying the primary's handoff state. When the primary's `/ready` returns HTTP 503 with `control_ready: true`, the standby treats the poll as successful and does not count it toward takeover.

A replacement router must run the same release, apply the same lifecycle rules, and keep saved ejections until those engines recover.

## 6. Monitor placement and control

See [Set up observability](../Observability.md) for generating engine scrape targets, starting Prometheus and Grafana, checking that metrics are arriving, and opening the dashboard over an SSH tunnel. The [dashboard reference](https://github.com/athrael-soju/Narwhal/blob/main/tools/observability/README.md#dashboard) covers choosing router and engine scopes.

The dashboard signals by subsystem:

| Signal                                         | What it tells you                                                     |
| ---------------------------------------------- | --------------------------------------------------------------------- |
| Readiness and lease epoch                      | Which router currently owns admission                                 |
| Pool size and normalized load                  | Whether either phase has hit its measured limit                       |
| Queue depth, pressure, and sheds               | Whether overload is queued, protected, or refused             |
| Ejection, probation, and role floors           | How much healthy capacity is left                                     |
| TTFT, TPOT, queue wait, and seat time          | Where client latency accumulates                                |
| Retries, failures, refusals, and rejections    | Which protection path is firing                                       |
| Role changes, reversals, and blocked decisions | Whether the controller holds a stable role assignment        |

Paging thresholds are deployment-specific. Set them in `tools/observability/prometheus-alerts.yml` for router down, engine down, error bursts, unserved requests, ejections, and role floors.

Use the request journals for one request, such as where it was placed and how long each stage took. Use metrics for process-level summaries, and allow for counter resets and for how long each metric retains state.
