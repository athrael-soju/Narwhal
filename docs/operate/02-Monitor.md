---
description: Interpret Narwhal router health, readiness and placement state during operation.
---

# Router state and placement monitoring

## 5. Interpreting router state

### `/health`

`/health` returns HTTP 200 in every router state with this body:

| Field | Value |
| --- | --- |
| `status` | Router state: `ok`, `degraded`, `maintenance`, `standby`, or `fenced` |
| `instances` | Configured fleet size |
| `available_instances` | Engines eligible for placement: the configured fleet minus ejected, draining, and quarantined engines |

### Backend exhaustion

When every engine is excluded from placement, the active router's `/health` reports `status: degraded` and `/ready` returns HTTP 503 with `reason: no available engines`. New completion requests receive HTTP 503 with the retryable error code `backend_unavailable`.

### Whole-wave lifecycle hold

Until whole-wave readmission succeeds, the active router's `/health` reports `status: maintenance` and `/ready` returns HTTP 503 with the lifecycle reason as `reason`. New completion requests receive HTTP 503 with error code `standby` and the lifecycle reason as the error message.

### Temporary holds

[Last-engine protection](../concepts/03-Failure-and-State.md#last-engine-protection) sets hold behavior for an engine that alone serves one of its roles.

### Router control and replacement

The HTTP status of `/ready` shows traffic eligibility, and load balancers route client traffic by it. `control_ready: true` in `/ready` shows a valid lease and healthy engine monitoring to the standby routers that copy the state handoff.

A replacement router:

- runs the same release
- applies the same lifecycle rules
- keeps saved ejections until recovery succeeds

## 6. Monitoring placement and control

Prometheus and Grafana setup: [Setting up observability](../Observability.md).

[Router metric](../telemetry/03-Metrics-and-Control.md#metric-families) signals:

| Signal | Operational question |
| --- | --- |
| Readiness and lease epoch | Which router admits traffic? |
| Pool size and normalized load | Has either phase reached its measured limit? |
| Queue depth, pressure, and sheds | Is overload waiting, protected, or refused? |
| Ejection, probation, and role floors | How much healthy capacity remains? |
| TTFT, TPOT, queue wait, and seat time | Where is client latency accumulating? |
| Retries, failures, refusals, and rejections | Which protection path is active? |
| Role changes, reversals, and blocked decisions | Is the role controller holding a stable role assignment? |

`tools/observability/prometheus-alerts.yml` defines these alert rules:

| Alert | Condition | Duration | Severity |
| --- | --- | :---: | --- |
| `NarwhalRouterDown` | Router scrape fails | 2m | `page` |
| `NarwhalEngineDown` | Engine scrape fails | 30s | `page` |
| `NarwhalEngineEjected` | At least one ejected engine | 1m | `page` |
| `NarwhalErrorBurst` | Failed requests above 0.5/s over 5m | 5m | `warn` |
| `NarwhalRejectedRising` | Rejected requests above 1% of offered requests over 5m | 5m | `warn` |
| `NarwhalExpiredRising` | Expired requests above 1% of offered requests over 5m | 5m | `warn` |
| `NarwhalRetryDeniedRising` | Retries denied by the shared retry quota above 1% of offered requests over 5m | 5m | `warn` |
| `NarwhalUnservedRising` | Phase placements where every eligible candidate exceeds the configured SLO, above 0.2/s over 10m | 10m | `warn` |
| `NarwhalPoolStarved` | A pool with zero engines | 2m | `warn` |
| `NarwhalPrefillBelowFloor` | Live prefill capacity below `min_prefill` | 1m | `warn` |
| `NarwhalDecodeBelowFloor` | Live decode capacity below `min_decode` | 1m | `warn` |

`NarwhalErrorBurst`, `NarwhalRejectedRising` and `NarwhalExpiredRising` sum their counter over its `reason` label, so each threshold applies to the whole outcome. The three share rules divide by `narwhal_offered_total`, which includes early refusals and rejections. Their 1% threshold is the share at which the dashboard's [**Dropped** headline](../observability/05-Dashboard.md#headline-row) turns yellow.

Request journals record per-request placement and timing. Metrics hold process-level summaries since the last counter reset.
