# Router state and placement monitoring

## 5. Interpret router state

### `/health`

`/health` returns HTTP 200 in every router state with this body:

- `status`: the router state, one of `ok`, `degraded`, `maintenance`, `standby`, or `fenced`
- `instances`: the configured fleet size
- `available_instances`: the number of engines eligible for placement, equal to the configured fleet minus ejected, draining, and quarantined engines

### Backend exhaustion

When every engine has been excluded from placement, the active router reports:

| Request | Response |
| --- | --- |
| `/health` | `status: degraded` |
| `/ready` | HTTP 503 with `reason: no available engines` |
| New completion requests | HTTP 503 and the retryable error code `backend_unavailable` |

### Whole-wave lifecycle hold

Until whole-wave readmission succeeds, the active router reports:

| Request | Response |
| --- | --- |
| `/health` | `status: maintenance` |
| `/ready` | HTTP 503 with the `reason` set to the lifecycle reason |
| New completion requests | HTTP 503 and the error code `standby`, with the lifecycle reason as the error message |

### Temporary holds

| Cause | Last eligible engine |
| --- | --- |
| Performance-drift hold | Stays in placement |
| Temporary-quarantine hold | Stays in placement |
| Failed health or inference probe | Leaves placement |

### Router control and replacement

`/ready` gives two signals:

| Signal | Meaning | Use |
| --- | --- | --- |
| HTTP status | Traffic eligibility | Load balancers route client traffic |
| `control_ready: true` | A valid lease and healthy engine monitoring | Standby routers copy the state handoff |

A replacement router:

- runs the same release;
- applies the same lifecycle rules;
- keeps saved ejections until recovery succeeds.

## 6. Monitor placement and control

Set up Prometheus and Grafana with [Set up observability](../Observability.md).

Each [dashboard](https://github.com/athrael-soju/Narwhal/blob/main/tools/observability/README.md#dashboard) signal answers one question:

| Signal                                         | Operational question                                     |
| ---------------------------------------------- | -------------------------------------------------------- |
| Readiness and lease epoch                      | Which router admits traffic?                             |
| Pool size and normalized load                  | Has either phase reached its measured limit?             |
| Queue depth, pressure, and sheds               | Is overload waiting, protected, or refused?              |
| Ejection, probation, and role floors           | How much healthy capacity remains?                       |
| TTFT, TPOT, queue wait, and seat time          | Where is client latency accumulating?                    |
| Retries, failures, refusals, and rejections    | Which protection path is active?                         |
| Role changes, reversals, and blocked decisions | Is the role controller holding a stable role assignment? |

Set paging thresholds in `tools/observability/prometheus-alerts.yml` for:

- router down;
- engine down;
- error bursts;
- unserved requests;
- ejections;
- role floors.

| Source | Scope |
| --- | --- |
| Request journals | Per-request placement and timing |
| Metrics | Process-level summaries since the last counter reset |
