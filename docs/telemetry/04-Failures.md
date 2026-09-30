# Monitoring and engine failure diagnosis

## Monitoring degradation

When monitoring passes keep failing, Narwhal marks itself degraded and stops admitting new requests. These metrics report the degradation.

| Metric                                          | Meaning                                                             | Labels  |
| ----------------------------------------------- | ------------------------------------------------------------------- | ------- |
| `narwhal_monitoring_degraded`                   | `1` while repeated monitoring failures are blocking new admissions. | none    |
| `narwhal_monitoring_core_consecutive_failures`  | Monitoring passes in a row in which at least one stage failed.      | none    |
| `narwhal_monitoring_core_failures_total`        | Failed monitoring passes since this router process started.         | none    |
| `narwhal_monitoring_stage_failures_total`       | Failures for one monitoring stage.                                  | `stage` |
| `narwhal_monitoring_stage_consecutive_failures` | Failures in a row for one monitoring stage.                         | `stage` |
| `narwhal_event_loop_lag_seconds`                | How far behind its latest scheduled deadline monitoring is running. | none    |
| `narwhal_event_loop_lag_high_water_seconds`     | The worst such delay this router process has seen.                  | none    |

The `stage` label is one of `controller`, `health`, `drains`, `rollover`, `readmission`, `liveness`, `handoff`, or `telemetry`. The `telemetry` stage refreshes floor state and writes loop logs. It does not export metrics.

## Engine breakers

Narwhal keeps a separate breaker for every combination of engine and failure class.

| Metric                             | Meaning                                                                           | Labels         |
| ---------------------------------- | --------------------------------------------------------------------------------- | -------------- |
| `narwhal_engine_breaker_streak`    | Failures in a row for one engine and failure class. Series stay exported at zero. | `iid`, `class` |
| `narwhal_engine_breaker_verifying` | `1` while a verification probe is running against the engine.                     | `iid`, `kind`  |

The failure classes are `connection`, `timeout`, `overload`, `inference_status`, `kv_handoff`, `stream`, and `liveness`. A verification probe is either `verify_health` or `verify_inference`. When a streak reaches `recovery.eject_after`, the failure class determines the action.

| Failure class      | Action at `recovery.eject_after`  |
| ------------------ | --------------------------------- |
| `connection`       | Eject the engine.                 |
| `timeout`          | Start a `verify_health` probe.    |
| `overload`         | Start a `verify_health` probe.    |
| `inference_status` | Start a `verify_inference` probe. |
| `kv_handoff`       | Start a `verify_inference` probe. |
| `stream`           | Start a `verify_inference` probe. |

The `liveness` class counts missed liveness sweeps.

A conclusive probe either clears the relevant failure streaks or ejects the engine (see `narwhal_ejected` on [Metrics and control](03-Metrics-and-Control.md)). An inconclusive probe, such as one that waited out the control connection pool, leaves the failure streak and any hold in place for a later probe to resolve.

## Alerts and dashboards

`make observe` stages the Prometheus alert rules in `tools/observability/prometheus-alerts.yml` and the Grafana dashboard from `tools/observability/grafana-narwhal.json`. The [dashboard README](https://github.com/athrael-soju/Narwhal/blob/main/tools/observability/README.md) describes what each panel covers and where its metrics stop being reliable.
