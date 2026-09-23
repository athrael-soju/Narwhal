# Monitoring and engine failure diagnosis

## Diagnose monitoring degradation

| Metric                                          | Meaning                                                                   | Labels  |
| ----------------------------------------------- | ------------------------------------------------------------------------- | ------- |
| `narwhal_monitoring_degraded`                   | `1` while repeated monitoring-pass failures block new admissions.         | none    |
| `narwhal_monitoring_core_consecutive_failures`  | Consecutive failed monitoring passes.                                     | none    |
| `narwhal_monitoring_core_failures_total`        | Failed monitoring passes during the current process lifetime.             | none    |
| `narwhal_monitoring_stage_failures_total`       | Failure count for a monitoring stage.                                     | `stage` |
| `narwhal_monitoring_stage_consecutive_failures` | Consecutive failures for a monitoring stage.                              | `stage` |
| `narwhal_event_loop_lag_seconds`                | Delay beyond the latest scheduled monitoring deadline.                    | none    |
| `narwhal_event_loop_lag_high_water_seconds`     | Largest monitoring-deadline delay observed by the current router process. | none    |

The stage label uses one of:

```text
controller
health
drains
rollover
readmission
liveness
handoff
telemetry
```

The `telemetry` stage performs floor-state refresh and loop logging.

## Diagnose engine breaker state

Narwhal maintains breaker state separately by engine and failure class.

| Metric                             | Meaning                                                                                    | Labels         |
| ---------------------------------- | ------------------------------------------------------------------------------------------ | -------------- |
| `narwhal_engine_breaker_streak`    | Consecutive failures for one engine and failure class. Zero-valued series remain exported. | `iid`, `class` |
| `narwhal_engine_breaker_verifying` | `1` while an engine verification probe is running.                                         | `iid`, `kind`  |

Breaker failure classes are:

```text
connection
timeout
overload
inference_status
kv_handoff
stream
liveness
```

Verification probes report one of:

```text
verify_health
verify_inference
```

When a probe completes, Narwhal either clears the relevant failure streaks or ejects the engine. Engine ejection is exported through `narwhal_ejected`.

`make observe` stages `tools/observability/prometheus-alerts.yml` for Prometheus rule evaluation and `tools/observability/grafana-narwhal.json` for Grafana dashboard provisioning.

[Dashboard definitions](https://github.com/athrael-soju/Narwhal/blob/main/tools/observability/README.md) documents the panel scope and metric boundaries.
