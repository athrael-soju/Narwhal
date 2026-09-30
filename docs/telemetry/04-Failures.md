# Engine monitoring and failure diagnosis

## Engine monitoring degradation

| Metric                                          | Type    | Labels  | Meaning                                                                   |
| ----------------------------------------------- | ------- | ------- | ------------------------------------------------------------------------- |
| `narwhal_monitoring_degraded`                   | gauge   |         | `1` when consecutive failed passes reach `controller.monitor_failure_limit` and the router blocks new admissions, otherwise `0`. |
| `narwhal_monitoring_core_consecutive_failures`  | gauge   |         | Consecutive failed monitoring passes.                                     |
| `narwhal_monitoring_core_failures_total`        | counter |         | Failed monitoring passes since process start.             |
| `narwhal_monitoring_stage_failures_total`       | counter | `stage` | Failed passes for one monitoring stage.                                   |
| `narwhal_monitoring_stage_consecutive_failures` | gauge   | `stage` | Consecutive failures for a monitoring stage.                              |
| `narwhal_event_loop_lag_seconds`                | gauge   |         | Delay beyond the latest scheduled monitoring deadline.                    |
| `narwhal_event_loop_lag_high_water_seconds`     | gauge   |         | Largest monitoring-deadline delay since router start. |

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

## Engine breaker state

| Metric                             | Type  | Labels         | Meaning                                                                                    |
| ---------------------------------- | ----- | -------------- | ------------------------------------------------------------------------------------------ |
| `narwhal_engine_breaker_streak`    | gauge | `iid`, `class` | Consecutive failures for one engine and failure class, exported at zero.  |
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
| Passes with a passing profile generation check   | The relevant failure streaks clear.                                |
| Fails                                                      | `narwhal_ejected` records the engine as ejected.                    |
| Times out in the local control pool                                 | Inconclusive, streaks unchanged.                |

`make observe` stages:

| Path                                        | Contents                                                                                                                                                                  |
| ------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `tools/observability/prometheus-alerts.yml` | Prometheus alert rules.                                                                                                                                                   |
| `tools/observability/grafana-narwhal.json`  | [Grafana dashboard](https://github.com/athrael-soju/Narwhal/blob/main/tools/observability/README.md). |
