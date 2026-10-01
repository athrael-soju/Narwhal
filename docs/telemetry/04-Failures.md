---
description: Metrics for engine monitoring degradation and engine breaker state in a Narwhal fleet.
---

# Engine monitoring and failure diagnosis

## Engine monitoring degradation

These metrics track failed monitoring passes and monitoring-deadline delay:

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `narwhal_monitoring_degraded` | gauge | | `1` while the router blocks new admissions after `controller.monitor_failure_limit` consecutive failed passes, otherwise `0`. |
| `narwhal_monitoring_core_consecutive_failures` | gauge | | Consecutive failed monitoring passes. |
| `narwhal_monitoring_core_failures_total` | counter | | Failed monitoring passes since process start. |
| `narwhal_monitoring_stage_failures_total` | counter | `stage` | Failed passes for one monitoring stage. |
| `narwhal_monitoring_stage_consecutive_failures` | gauge | `stage` | Consecutive failures for a monitoring stage. |
| `narwhal_event_loop_lag_seconds` | gauge | | Delay beyond the latest scheduled monitoring deadline. |
| `narwhal_event_loop_lag_high_water_seconds` | gauge | | Largest monitoring-deadline delay since router start. |

`stage` values:

```text
controller
health
drains
rollover
readmission
liveness
residency
handoff
telemetry
```

The `telemetry` stage covers floor-state refresh and loop logs.

## Engine breaker state

These metrics track each engine's failure streaks, verification probes, and quarantine:

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `narwhal_engine_breaker_streak` | gauge | `iid`, `class` | Current consecutive-failure streak for every configured engine and failure class. |
| `narwhal_engine_breaker_verifying` | gauge | `iid`, `kind` | `1` while an engine verification probe is running. |
| `narwhal_engine_quarantined` | gauge | `iid` | `1` while a failure quarantine or inference-probe hold keeps the engine out of placement. |

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

A verification probe ends in one of four ways:

- A pass with a passing profile generation check clears the relevant failure streaks.
- A failure on a [covered engine](../concepts/03-Failure-and-State.md#failure-evidence) ejects the engine, and `narwhal_ejected` records it.
- A failure on an uncovered engine lifts its hold and keeps the engine in placement.
- A probe that waits out the local control pool is inconclusive and leaves the streaks unchanged.
