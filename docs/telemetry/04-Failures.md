# Engine monitoring and failure diagnosis

## Engine monitoring degradation

| Metric                                          | Type    | Labels  | Meaning                                                                   |
| ----------------------------------------------- | ------- | ------- | ------------------------------------------------------------------------- |
| `narwhal_monitoring_degraded`                   | gauge   |         | `1` once consecutive failed passes reach `controller.monitor_failure_limit`. |
| `narwhal_monitoring_core_consecutive_failures`  | gauge   |         | Consecutive failed monitoring passes.                                     |
| `narwhal_monitoring_core_failures_total`        | counter |         | Failed monitoring passes during the current process lifetime.             |
| `narwhal_monitoring_stage_failures_total`       | counter | `stage` | Failed passes for one monitoring stage.                                   |
| `narwhal_monitoring_stage_consecutive_failures` | gauge   | `stage` | Consecutive failures for a monitoring stage.                              |
| `narwhal_event_loop_lag_seconds`                | gauge   |         | Delay beyond the latest scheduled monitoring deadline.                    |
| `narwhal_event_loop_lag_high_water_seconds`     | gauge   |         | Largest monitoring-deadline delay observed by the current router process. |

`stage` values:

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

The `telemetry` stage covers floor-state refresh and loop logs.

While monitoring is degraded, the router blocks new admissions.

## Engine breaker state

| Metric                             | Type  | Labels         | Meaning                                                                                    |
| ---------------------------------- | ----- | -------------- | ------------------------------------------------------------------------------------------ |
| `narwhal_engine_breaker_streak`    | gauge | `iid`, `class` | Consecutive failures for one engine and failure class, with zero-valued series exported.  |
| `narwhal_engine_breaker_verifying` | gauge | `iid`, `kind`  | `1` while an engine verification probe is running.                                         |

`class` values:

```text
connection
timeout
overload
inference_status
kv_handoff
stream
liveness
```

`kind` values: `verify_health` or `verify_inference`.

| Verification probe result                                  | Effect                                                              |
| ---------------------------------------------------------- | ------------------------------------------------------------------- |
| Passes, and the engine's profile generation check passes   | The relevant failure streaks clear.                                |
| Fails                                                      | `narwhal_ejected` records the engine as ejected.                    |
| Waits out the control pool                                 | Inconclusive, with the streaks holding their values.                |

`make observe` stages these assets:

| Path                                        | Contents                                                                                                                                                                  |
| ------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `tools/observability/prometheus-alerts.yml` | Prometheus alert rules.                                                                                                                                                   |
| `tools/observability/grafana-narwhal.json`  | Grafana dashboard, with panels and metrics listed in the [observability asset contracts](https://github.com/athrael-soju/Narwhal/blob/main/tools/observability/README.md). |
