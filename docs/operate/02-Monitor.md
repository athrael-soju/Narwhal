# Router state and placement monitoring

## 5. Interpret router state

`/health` and `/ready` answer different questions. `/health` describes the router process and the fleet behind it. `/ready` tells the load balancer whether this router should get traffic.

### `/health`

`/health` keeps reporting on the router process even when the backends are down, and it returns HTTP 200 throughout. The body has two fleet counts: `instances` is the configured fleet size, and `available_instances` is how many engines are currently eligible for placement once ejected, draining, and quarantined engines are left out.

### Backend exhaustion

Once every engine has been excluded from placement, the active router's `/health` reports `status: degraded` and `/ready` returns HTTP 503 with `reason: no available engines`. New completion requests fail with `backend_unavailable`, which is a retryable error.

### Whole-wave lifecycle hold

During a whole-wave lifecycle hold, `/health` reports `status: maintenance`, `/ready` returns HTTP 503 with the hold as its `reason`, and completion requests get HTTP 503 with the error code `standby`. Background monitoring stops until the wave is readmitted.

### Temporary holds

Performance-drift and temporary-quarantine holds never take out the last eligible engine; it stays in service. A failed health or inference probe can still exclude it, though.

### Router control and replacement

A backend outage or a managed maintenance window doesn't cost the active router control, and client load balancers keep routing on HTTP status the whole time. The standby, meanwhile, keeps copying the primary's handoff state: when the primary's `/ready` returns HTTP 503 with `control_ready: true`, the standby treats the poll as successful rather than counting it toward takeover.

If you replace a router, the new one has to run the same release and apply the same lifecycle rules. It also has to keep any saved ejections in place until those engines recover.

## 6. Monitor placement and control

[Set up observability](../Observability.md) walks through generating engine scrape targets, starting Prometheus and Grafana, checking that metrics are arriving, and opening the dashboard over an SSH tunnel. The [dashboard reference](https://github.com/athrael-soju/Narwhal/blob/main/tools/observability/README.md#dashboard) explains how to choose router and engine scopes.

The dashboard is easiest to read one subsystem at a time:

| Signal                                         | What it tells you                                                     |
| ---------------------------------------------- | --------------------------------------------------------------------- |
| Readiness and lease epoch                      | Which router currently owns admission                                 |
| Pool size and normalized load                  | Whether either phase has hit its measured limit                       |
| Queue depth, pressure, and sheds               | Whether overload is waiting in the queue, being protected, or refused |
| Ejection, probation, and role floors           | How much healthy capacity is left                                     |
| TTFT, TPOT, queue wait, and seat time          | Where client latency is building up                                   |
| Retries, failures, refusals, and rejections    | Which protection path is firing                                       |
| Role changes, reversals, and blocked decisions | Whether the controller has settled on a stable role assignment        |

Paging thresholds depend on the deployment, so you'll need to set them yourself in `tools/observability/prometheus-alerts.yml`. Configure thresholds there for router down, engine down, error bursts, unserved requests, ejections, and role floors.

For questions about one request, such as where it was placed and how long each stage took, go to the request journals. Metrics are better for process-level summaries, as long as you allow for counter resets and for how long each metric retains state.
