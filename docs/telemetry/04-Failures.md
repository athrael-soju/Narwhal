---
description: Metrics for engine monitoring degradation and engine breaker state in a Narwhal fleet.
---

# Engine monitoring and failure diagnosis

## Engine monitoring degradation

These metrics track failed monitoring passes, monitoring-deadline delay and event-loop CPU time:

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `narwhal_monitoring_degraded` | gauge | | `1` while the router blocks new admissions after `controller.monitor_failure_limit` consecutive failed passes, otherwise `0`. |
| `narwhal_monitoring_core_consecutive_failures` | gauge | | Consecutive failed monitoring passes. |
| `narwhal_monitoring_core_failures_total` | counter | | Failed monitoring passes since process start. |
| `narwhal_monitoring_stage_failures_total` | counter | `stage` | Failed passes for one monitoring stage. |
| `narwhal_monitoring_stage_consecutive_failures` | gauge | `stage` | Consecutive failures for a monitoring stage. |
| `narwhal_event_loop_lag_seconds` | gauge | | Delay beyond the latest scheduled monitoring deadline. |
| `narwhal_event_loop_lag_high_water_seconds` | gauge | | Largest monitoring-deadline delay since router start. |
| `narwhal_event_loop_busy_seconds_total` | counter | | CPU seconds the router's event-loop thread used since the monitoring loop started. |

`rate(narwhal_event_loop_busy_seconds_total[5m])` is the share of wall time the event-loop thread spends on CPU, from `0` to `1`.

The router updates the counter each time the monitoring loop wakes, at a scheduled monitoring deadline or on an urgent control wake.

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

These metrics track each engine's failure streaks, verification probes, and placement holds:

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `narwhal_engine_breaker_streak` | gauge | `iid`, `class` | Current consecutive-failure streak for every configured engine and failure class. |
| `narwhal_engine_breaker_verifying` | gauge | `iid`, `kind` | `1` while an engine verification probe is running. |
| `narwhal_engine_quarantined` | gauge | `iid` | `1` while a hold of either kind keeps the engine out of placement. |
| `narwhal_engine_held` | gauge | `iid`, `kind` | `1` while a hold of this kind, `timed` or `inference`, keeps the engine out of placement. |

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

`kind` values on `narwhal_engine_breaker_verifying`: `verify_health` or `verify_inference`.

[Failure evidence](../concepts/03-Failure-and-State.md#failure-evidence) maps each class to the action the breaker takes at `recovery.eject_after` consecutive failures.

### Dropped connections

A dropped connection is an engine connection that closes or resets after the router sent the request, before the response body completes. The router classifies it as follows:

- The breaker counts `timeout` evidence against the engine, on prefill and decode legs alike, and runs a health probe at `recovery.eject_after`.
- A dropped connection starts no inference hold.
- The request's [outcome reason](01-Journal.md#outcome-reasons) is `engine_connection`.
- With `recovery.failure_quarantine_s` above `0`, a [covered engine](../concepts/03-Failure-and-State.md#failure-evidence) enters a timed quarantine.

Two neighbouring failures carry other classes. A refused connection or connect timeout counts as `connection` evidence, which ejects the engine at `recovery.eject_after`. A decode response body that completes without the `[DONE]` terminator counts as `stream` evidence, which starts an inference hold.

### Hold kinds

A hold keeps a covered engine out of new placement. `/narwhal/state` lists every held engine in `quarantined` and separates the two kinds in [`holds`](../http-api/05-Live-State.md#holds):

| Kind | Starts when | Ends when |
| --- | --- | --- |
| `timed` | A request leg fails and `recovery.failure_quarantine_s` is above `0`. A local pool wait and a 4xx response other than 408 or 429 start no hold. | The deadline passes, or earlier on a health or inference verification that meets the profile-match rule. |
| `inference` | A `stream`, `inference_status`, or `kv_handoff` streak reaches `recovery.eject_after`. | An inference probe passes and meets the profile-match rule. The hold has no deadline. |

An inference hold replaces a running timed quarantine on the same engine. A hold of either kind also ends when the engine is ejected or drained, or when another engine's ejection or drain leaves it uncovered.

While an inference hold lasts, engine monitoring repeats the inference probe every `recovery.readmit_every` monitor intervals.

### Inference probe producers

Each `stream` failure on a decode leg records the producer of its KV transfer against the decode engine. `holds.inference[].recorded_producers` lists them. The inference probe verifies each recorded producer's path and tries producers in this order:

1. The recorded producer, while it is configured and live: not ejected, held, or draining.
2. One other live engine that places prefill legs, choosing the fewest resident prefill requests and then engine ID order. Prefill-role engines place prefill legs; when none is live, unpinned engines do.
3. A standalone probe, which runs both legs on the held engine without a KV transfer.

When a producer's prefill leg fails, the probe has tested nothing on the held engine. The verifier records a `producer_failed` outcome and defers to the next producer in the list. A producer leg that waits out the local control pool is inconclusive instead: the verifier keeps the hold and retries at the next probe.

After every recorded path passes, through any producer, and the profile-match rule holds, the verifier clears the recorded producers and lifts the hold. An engine without a recorded producer, such as one held for `inference_status` evidence alone, verifies through a standalone probe.

### Verification probe outcomes

`narwhal_engine_probes_total` counts verification probe outcomes by `iid`, `kind` and `outcome`:

| `outcome` | `kind` | Meaning |
| --- | --- | --- |
| `passed` | both | `/health` answered, or both inference legs passed. Under the profile-match rule the probed failure evidence clears; a profile mismatch ejects the engine with cause `profile_generation`. |
| `failed` | both | `/health` did not answer, or a leg on the probed engine failed. A covered engine is ejected; an uncovered engine leaves any hold and stays in placement. |
| `inconclusive` | both | A probe leg waited out the local control pool. The hold and streaks stay unchanged. |
| `producer_failed` | `verify_inference` | The producer's prefill leg failed. The next producer runs the probe. |
| `unavailable` | `verify_inference` | The router has no model name to probe with. The hold stays. |

### Ejections, holds and readmissions

These counters accumulate from router start:

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `narwhal_engine_ejections_total` | counter | `iid`, `cause` | Ejections of the engine. |
| `narwhal_engine_hold_starts_total` | counter | `iid`, `kind` | Holds started on the engine. |
| `narwhal_engine_hold_ends_total` | counter | `iid`, `kind`, `cause` | Holds that ended on the engine. |
| `narwhal_engine_readmissions_total` | counter | `iid`, `evidence` | Returns of the ejected engine to placement. |

Ejection `cause` values:

| `cause` | Evidence |
| --- | --- |
| `connection` | The `connection` streak reached `recovery.eject_after`. |
| `liveness` | `/health` stayed silent for `recovery.liveness_misses` consecutive sweeps. |
| `health_probe` | A health verification probe failed on a covered engine. |
| `inference_probe` | An inference verification probe failed on a covered engine. |
| `drift` | Decode drift stayed outside the engine's band. |
| `process_identity` | The engine's process identity changed or could not be read. |
| `profile_generation` | The loaded profiles do not match the live process generation. |

Hold-end `cause` values:

| `cause` | The hold ended because |
| --- | --- |
| `expired` | The timed quarantine deadline passed. |
| `superseded` | An inference hold replaced the timed quarantine. |
| `health` | A health answer met the profile-match rule. Inference holds ignore it. |
| `verification` | An inference probe passed and met the profile-match rule. |
| `lifecycle` | Lifecycle readmission validated the engine. |
| `ejected` | The engine was ejected. |
| `drained` | An operator drain removed the engine from placement. |
| `uncovered` | No other live engine places the engine's roles. |

Readmission `evidence` values are `health`, `verification`, and `lifecycle`. An ejected [inference suspect](../concepts/03-Failure-and-State.md#failure-evidence) returns only on `verification` or `lifecycle`.

Each transition also writes a [journal event](01-Journal.md#journal-events): `engine_ejected`, `engine_hold_started`, `engine_hold_ended`, `engine_probe`, and `engine_readmitted`.
