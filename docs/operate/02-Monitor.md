# Router state and placement monitoring

## 5. Interpret router state

### `/health`

`/health` returns HTTP 200 in every router state, including backend outages. The body has three fields:

- `status`: the current router state, one of `ok`, `degraded`, `maintenance`, `standby`, or `fenced`
- `instances`: the configured fleet size
- `available_instances`: the number of engines still eligible for placement. Ejected, draining, and quarantined engines are left out

### Backend exhaustion

When every engine has been excluded from placement, the active router reports:

| Request | Response |
| --- | --- |
| `/health` | `status: degraded` |
| `/ready` | HTTP 503 with `reason: no available engines` |
| New completion requests | HTTP 503 and the retryable error code `backend_unavailable` |

### Whole-wave lifecycle hold

A whole-wave lifecycle hold puts the router in `status: maintenance` and pauses background engine monitoring until whole-wave readmission succeeds. The status carries the lifecycle reason. The endpoints respond as follows:

| Request | Response |
| --- | --- |
| `/health` | `status: maintenance` |
| `/ready` | HTTP 503 with the `reason` set to the lifecycle reason |
| New completion requests | HTTP 503 and the error code `standby`, with the lifecycle reason as the error message |

### Temporary holds

Performance-drift and temporary-quarantine holds keep the last eligible engine in placement. Failed health or inference probes exclude it.

### Router control and replacement

The active router keeps control of the fleet through backend outages and managed maintenance. `/ready` gives two signals:

| Signal | Meaning | Use |
| --- | --- | --- |
| HTTP status | Traffic eligibility | Load balancers route client traffic |
| `control_ready: true` | A valid lease and healthy engine monitoring | Standby routers copy the state handoff |

A replacement router must run the same release, apply the same lifecycle rules. It keeps saved ejections until recovery succeeds.

## 6. Monitor placement and control

Set up Prometheus and Grafana with [Set up observability](../Observability.md). The [dashboard reference](https://github.com/athrael-soju/Narwhal/blob/main/tools/observability/README.md#dashboard) lists the router and engine scopes.

Each dashboard signal answers one question:

| Signal                                         | Operational question                                     |
| ---------------------------------------------- | -------------------------------------------------------- |
| Readiness and lease epoch                      | Which router admits traffic?                             |
| Pool size and normalized load                  | Has either phase reached its measured limit?             |
| Queue depth, pressure, and sheds               | Is overload waiting, protected, or refused?              |
| Ejection, probation, and role floors           | How much healthy capacity remains?                       |
| TTFT, TPOT, queue wait, and seat time          | Where is client latency accumulating?                    |
| Retries, failures, refusals, and rejections    | Which protection path is active?                         |
| Role changes, reversals, and blocked decisions | Is the role controller holding a stable role assignment? |

Set paging thresholds in `tools/observability/prometheus-alerts.yml`. Alert on router down, engine down, error bursts, unserved requests, ejections, and role floors. Use request journals for per-request placement and timing. Metrics give process-level summaries and reset with their counters.
