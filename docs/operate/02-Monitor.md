---
description: Interpret Narwhal router health, readiness and placement state during operation.
---

# Router state and placement monitoring

## 5. Interpret router state

### `/health`

`/health` returns HTTP 200 in every router state with this body:

| Field | Value |
| --- | --- |
| `status` | Router state: `ok`, `degraded`, `maintenance`, `standby`, or `fenced` |
| `instances` | Configured fleet size |
| `available_instances` | Engines eligible for placement: the configured fleet minus ejected, draining, and quarantined engines |

### Backend exhaustion

Active router responses when every engine is excluded from placement:

| Request | Response |
| --- | --- |
| `/health` | `status: degraded` |
| `/ready` | HTTP 503 with `reason: no available engines` |
| New completion requests | HTTP 503, retryable error code `backend_unavailable` |

### Whole-wave lifecycle hold

Active router responses until whole-wave readmission succeeds:

| Request | Response |
| --- | --- |
| `/health` | `status: maintenance` |
| `/ready` | HTTP 503 with `reason` = lifecycle reason |
| New completion requests | HTTP 503, error code `standby`, error message = lifecycle reason |

### Temporary holds

Hold behavior for an engine that [alone places one of its roles](../concepts/03-Failure-and-State.md#last-engine-protection):

| Cause | Result |
| --- | --- |
| Performance-drift hold | Remains in placement |
| Temporary-quarantine hold | Remains in placement |
| Inference-probe hold | Remains in placement |
| Temporary-quarantine or inference-probe hold after another engine's ejection or drain | Returns to placement |
| Failed health or inference probe | Remains in placement |
| Connection-error or liveness ejection | Leaves placement |

### Router control and replacement

Signals in `/ready`:

| Signal | Meaning | Use |
| --- | --- | --- |
| HTTP status | Traffic eligibility | Load balancers route client traffic |
| `control_ready: true` | A valid lease and healthy engine monitoring | Standby routers copy the state handoff |

A replacement router:

- runs the same release
- applies the same lifecycle rules
- keeps saved ejections until recovery succeeds

## 6. Monitor placement and control

Prometheus and Grafana setup: [Set up observability](../Observability.md).

[Dashboard](../observability/05-Dashboard.md) signals:

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
| --- | --- | --- | --- |
| `NarwhalRouterDown` | Router scrape fails | 2m | `page` |
| `NarwhalEngineDown` | Engine scrape fails | 30s | `page` |
| `NarwhalEngineEjected` | At least one ejected engine | 1m | `page` |
| `NarwhalErrorBurst` | Failed requests above 0.5/s over 5m | 5m | `warn` |
| `NarwhalUnservedRising` | Unserved requests above 0.2/s over 10m | 10m | `warn` |
| `NarwhalPoolStarved` | A pool with zero engines | 2m | `warn` |
| `NarwhalPrefillBelowFloor` | Live prefill capacity below `min_prefill` | 1m | `warn` |
| `NarwhalDecodeBelowFloor` | Live decode capacity below `min_decode` | 1m | `warn` |

Telemetry sources:

| Source | Scope |
| --- | --- |
| Request journals | Per-request placement and timing |
| Metrics | Process-level summaries since the last counter reset |
