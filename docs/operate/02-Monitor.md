# Router state and placement monitoring

## 5. Interpret router state

Use `/health` for process and fleet state. Use `/ready` for traffic eligibility.

### `/health`

`/health` continues reporting router-process health through backend outages.

It returns HTTP 200 with:

- `instances`: configured fleet size;
- `available_instances`: engines currently eligible for placement after ejection, drain, and quarantine exclusions.

### Backend exhaustion

When every engine has been excluded from placement, the active router reports:

```text
status: degraded
```

`/ready` returns HTTP 503 with:

```text
reason: no available engines
```

New completion requests receive a retryable:

```text
backend_unavailable
```

### Whole-wave lifecycle hold

A whole-wave lifecycle hold places the router in:

```text
status: maintenance
```

The status includes the lifecycle reason. Completion requests receive:

```text
standby
```

at HTTP 503.

### Temporary holds

Performance-drift and temporary-quarantine holds retain the last eligible engine in service. Failed health or inference probes may still exclude that engine.

During backend outages and managed maintenance, the router retains control.

A whole-wave hold pauses background monitoring until readmission.

Standby routers use `control_ready` to copy the current handoff state.

Client load balancers continue routing according to HTTP status.

A replacement router must run the same release, apply the same lifecycle rules, and preserve saved ejections until recovery succeeds.

## 6. Monitor placement and control

Use [Set up observability](../Observability.md) to:

1. generate engine scrape targets;
2. start Prometheus and Grafana;
3. verify metric collection;
4. open the dashboard through an SSH tunnel.

Choose router and engine scopes using the [dashboard reference](https://github.com/athrael-soju/Narwhal/blob/main/tools/observability/README.md#dashboard).

Read the dashboard by subsystem:

| Signal                                         | Operational question                                |
| ---------------------------------------------- | --------------------------------------------------- |
| Readiness and lease epoch                      | Which router owns admission?                        |
| Pool size and normalized load                  | Has either phase reached its measured limit?        |
| Queue depth, pressure, and sheds               | Is overload waiting, protected, or refused?         |
| Ejection, probation, and role floors           | How much healthy capacity remains?                  |
| TTFT, TPOT, queue wait, and seat time          | Where is client latency accumulating?               |
| Retries, failures, refusals, and rejections    | Which protection path is active?                    |
| Role changes, reversals, and blocked decisions | Is the controller holding a stable role assignment? |

Set deployment-specific paging thresholds in:

```text
tools/observability/prometheus-alerts.yml
```

Configure thresholds for:

- router down;
- engine down;
- error bursts;
- unserved requests;
- ejections;
- role floors.

Use request journals for per-request placement and timing analysis.

Use metrics for process-level summaries. Interpret counters within their reset behaviour and state-retention windows.
