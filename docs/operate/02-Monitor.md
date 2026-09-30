# Router state and placement monitoring

## 5. Interpret router state

### `/health`

`/health` returns HTTP 200 in every router state with this body:

- `status`: router state, one of `ok`, `degraded`, `maintenance`, `standby`, or `fenced`
- `instances`: the configured fleet size
- `available_instances`: engines eligible for placement, the configured fleet minus ejected, draining, and quarantined engines

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

| Cause | Last eligible engine |
| --- | --- |
| Performance-drift hold | Remains in placement |
| Temporary-quarantine hold | Remains in placement |
| Failed health or inference probe | Removed from placement |

### Router control and replacement

Signals in `/ready`:

| Signal | Meaning | Use |
| --- | --- | --- |
| HTTP status | Traffic eligibility | Load balancers route client traffic |
| `control_ready: true` | A valid lease and healthy engine monitoring | Standby routers copy the state handoff |

A replacement router:

- Runs the same release.
- Applies the same lifecycle rules.
- Keeps saved ejections until recovery succeeds.

## 6. Monitor placement and control

Prometheus and Grafana setup: [Set up observability](../Observability.md).

[Dashboard](https://github.com/athrael-soju/Narwhal/blob/main/tools/observability/README.md#dashboard) signals:

| Signal                                         | Operational question                                     |
| ---------------------------------------------- | -------------------------------------------------------- |
| Readiness and lease epoch                      | Which router admits traffic?                             |
| Pool size and normalized load                  | Has either phase reached its measured limit?             |
| Queue depth, pressure, and sheds               | Is overload waiting, protected, or refused?              |
| Ejection, probation, and role floors           | How much healthy capacity remains?                       |
| TTFT, TPOT, queue wait, and seat time          | Where is client latency accumulating?                    |
| Retries, failures, refusals, and rejections    | Which protection path is active?                         |
| Role changes, reversals, and blocked decisions | Is the role controller holding a stable role assignment? |

Paging thresholds in `tools/observability/prometheus-alerts.yml` for:

- Router down.
- Engine down.
- Error bursts.
- Unserved requests.
- Ejections.
- Role floors.

| Source | Scope |
| --- | --- |
| Request journals | Per-request placement and timing |
| Metrics | Process-level summaries since the last counter reset |
