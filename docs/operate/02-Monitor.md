---
description: Interpret Narwhal router health, readiness and placement state during operation.
---

# Router state and placement monitoring

## Interpreting router state

### `/health`

`/health` returns HTTP 200 in every router state with this body:

| Field | Value |
| --- | --- |
| `status` | Router state: `ok` when the router admits traffic, `fenced` after a failover block or with an invalid lease, `maintenance` during a whole-wave hold, `standby` on a standby, otherwise `degraded` |
| `instances` | Engines in the [membership snapshot](../http-api/08-Engine-Membership.md) |
| `available_instances` | Engines eligible for placement: the membership minus ejected, draining, quarantined, and role-switching engines |

### Backend exhaustion

When every engine is excluded from placement, the active router's `/health` reports `status: degraded` and `/ready` returns HTTP 503 with `reason: no available engines`. New completion requests receive HTTP 503 with the retryable error code `backend_unavailable` and the message `No engines are available`.

### Whole-wave lifecycle hold

Until whole-wave readmission succeeds, the active router's `/health` reports `status: maintenance` and `/ready` returns HTTP 503 with the lifecycle reason, such as `whole-wave drain <id>`, as `reason`. New completion requests receive HTTP 503 with error code `not_ready`, the message `Router is not ready` and the lifecycle reason as `reason`. The journal's terminal row for each such request carries the lifecycle reason as `readiness_reason`.

### Temporary holds

[Last-engine protection](../concepts/03-Failure-and-State.md#last-engine-protection) keeps the last engine of a role in placement.

### Router control and replacement

Load balancers route traffic by the HTTP status of [`/ready`](../http-api/04-Inspection.md#get-ready). A standby copies the primary's state handoff even while the primary's `/ready` returns HTTP 503.

## Monitoring placement and control

To set up Prometheus and Grafana, follow [Setting up observability](../Observability.md).

Use the [router metrics](../telemetry/03-Metrics-and-Control.md#metric-families) to answer these questions:

| Signal | Operational question |
| --- | --- |
| Readiness and lease epoch | Which router admits traffic? |
| Pool size and normalized load | Has either phase reached its measured limit? |
| Queue depth, pressure, and sheds | Is overload waiting, protected, or refused? |
| Ejection, probation, and role floors | How much healthy capacity remains? |
| TTFT, TPOT, queue wait, and seat time | Where is client latency accumulating? |
| Retries, failures, refusals, and rejections | Which protection path is active? |
| Role changes, reversals, and blocked decisions | Is the role controller holding a stable role assignment? |

`src/narwhal/observability/stack/prometheus-alerts.yml` defines these alert rules:

| Alert | Condition | Duration | Severity |
| --- | --- | :---: | --- |
| `NarwhalRouterDown` | A router scrape fails while no router is [active](../telemetry/03-Metrics-and-Control.md#reading-router-lease-gauges) | 2m | `page` |
| `NarwhalStandbyRouterDown` | A router scrape fails while another router is active | 2m | `warn` |
| `NarwhalNoRouterReady` | Prometheus scrapes more than one router, none reports `narwhal_router_ready` `1`, and `NarwhalRouterDown` does not cover it | 1m | `page` |
| `NarwhalMultipleRoutersReady` | More than one router reports `narwhal_router_ready` `1` | 30s | `page` |
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

`NarwhalErrorBurst`, `NarwhalRejectedRising` and `NarwhalExpiredRising` sum their counter over its `reason` label, so each threshold applies to the whole outcome. The request rate rules also sum over the routers of a pair, since each router counts the requests it answered. No rule reads `narwhal_standby_refusals_total`, which counts the requests a standby refuses while its last probe found its primary controlling the fleet, because the load balancer retries those on the primary. Every other refusal counts in `narwhal_rejected_total`, including those of a standby that waits for a killed primary's lease to expire and of a fenced or single router.

A stopped single router raises `NarwhalRouterDown` only, and a single router that is not ready raises no router alert. The router rules count only samples scraped in the last 30 seconds.

`NarwhalEngineEjected`, `NarwhalPoolStarved`, `NarwhalPrefillBelowFloor` and `NarwhalDecodeBelowFloor` read the router with the highest `narwhal_router_lease_epoch` among the routers scraped in the last 30 seconds: the lease holder, or the router that held the lease last.

`NarwhalRejectedRising`, `NarwhalExpiredRising` and `NarwhalRetryDeniedRising` divide by `narwhal_offered_total`, which includes early refusals and rejections. Their 1% threshold is the share at which the dashboard's [**Dropped** headline](../observability/05-Dashboard.md#headline-row) turns yellow.

To see the placement and timing of one request, read the [request journal](../telemetry/01-Journal.md#diagnosing-a-request-from-the-journal).
