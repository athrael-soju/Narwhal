---
description: Metrics for engine monitoring degradation and engine breaker state in a Narwhal fleet.
---

# Engine monitoring and failure diagnosis

## Engine monitoring degradation

The router runs a monitoring pass every `controller.monitor_interval_s` seconds. A stage that raises an exception counts as a failure, and the rest of the pass continues. After `controller.monitor_failure_limit` consecutive passes with a failed stage, the router admits no new requests until a pass completes with every stage successful.

These metrics track failed passes, monitoring-deadline delay and event-loop CPU time:

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `narwhal_monitoring_degraded` | gauge | | `1` while failed passes block new admissions, otherwise `0`. |
| `narwhal_monitoring_core_consecutive_failures` | gauge | | Consecutive passes with a failed stage. |
| `narwhal_monitoring_core_failures_total` | counter | | Passes with a failed stage since router start. |
| `narwhal_monitoring_stage_failures_total` | counter | `stage` | Failures of one stage since router start. |
| `narwhal_monitoring_stage_consecutive_failures` | gauge | `stage` | Consecutive failures of one stage. |
| `narwhal_event_loop_lag_seconds` | gauge | | Delay beyond the latest scheduled monitoring deadline. |
| `narwhal_event_loop_lag_high_water_seconds` | gauge | | Largest monitoring-deadline delay since router start. |
| `narwhal_event_loop_busy_seconds_total` | counter | | CPU seconds the router's event-loop thread used since the monitoring loop started. |

`rate(narwhal_event_loop_busy_seconds_total[5m])` is the share of wall time the event-loop thread spends on CPU, from `0` to `1`. The router updates the counter each time the monitoring loop wakes, at a scheduled monitoring deadline or on an urgent control wake.

Each `stage` value names the work that failed:

| `stage` | Work |
| --- | --- |
| `controller` | Role controller evaluation, on each pass and on urgent control wakes |
| `health` | Decode drift scoring and drift ejections |
| `drains` | Closing the drain of each role change whose work has finished |
| `rollover` | Publishing the current monitoring interval's measurements and opening the next interval |
| `readmission` | Repeat inference probes, readmission of due ejected engines, and on vLLM [peer memory release](../concepts/03-Failure-and-State.md#peer-memory-release) |
| `transfer_refresh` | On SGLang with engine-side role switching, [transfer state refresh](../concepts/03-Failure-and-State.md#transfer-state-refresh) |
| `liveness` | The sweep of each engine's health route, `/health` on vLLM and `/ready` on SGLang, every `recovery.liveness_every` monitor intervals in its own loop |
| `residency` | Refresh of each engine's residency view from its attestation sidecar, every monitor interval in its own loop |
| `handoff` | Writing the state handoff |
| `telemetry` | Prefill floor-state refresh and the loop log line |

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

For the action the breaker takes on each class at `recovery.eject_after` consecutive failures, and for the profile-match rule, read [Failure evidence](../concepts/03-Failure-and-State.md#failure-evidence).

### Dropped connections

A dropped connection is an engine connection that closes or resets after the router sent the request and before the response body completes. The breaker counts it as `timeout` evidence on prefill and decode legs alike, so `recovery.eject_after` dropped connections in a row start a health probe and no inference hold. The request's [outcome reason](01-Journal.md#outcome-reasons) is `engine_connection`, and with `recovery.failure_quarantine_s` above `0` a [covered engine](../concepts/03-Failure-and-State.md#failure-evidence) enters a timed quarantine.

A refused connection or a connect timeout counts as `connection` evidence instead, and its streak ejects the engine at `recovery.eject_after`. A decode response body that ends without the `[DONE]` terminator counts as `stream` evidence, and its streak starts an inference hold at `recovery.eject_after`.

### Hold kinds

A hold keeps a covered engine out of new placement. `/narwhal/state` lists every held engine in `quarantined` and separates the two kinds in [`holds`](../http-api/05-Live-State.md#holds):

| Kind | Starts when | Ends when |
| --- | --- | --- |
| `timed` | A request leg fails and `recovery.failure_quarantine_s` is above `0`. A local pool wait and an engine 4xx response other than 408 or 429 start no hold. | The deadline passes, or earlier when a health-route answer or a passing inference probe meets the profile-match rule. |
| `inference` | A `stream`, `inference_status`, or `kv_handoff` streak reaches `recovery.eject_after`. | An inference probe passes every recorded path and the profile-match rule holds. The hold has no deadline. |

An inference hold replaces a running timed quarantine on the same engine. A hold of either kind also ends when the engine is ejected or drained, when lifecycle readmission validates it, or when another engine's ejection or drain leaves it uncovered. An uncovered engine whose streak calls for an inference probe stays in placement while the probe runs.

The first inference probe starts when the streak reaches `recovery.eject_after`. While the engine remains an inference suspect, outside ejection and drain, engine monitoring repeats the probe every `recovery.readmit_every` monitor intervals.

### Inference probe producers

Each `stream` failure on a decode leg records the leg's producer against the decode engine, and `holds.inference[].recorded_producers` lists those producers. The inference probe retests each recorded path in turn. The hold lifts once every recorded path has passed, through any producer, and the profile-match rule holds. A path recorded while a probe runs waits for the next probe.

On vLLM, the probe of each path tries producers in this order:

1. The recorded producer, while it is live and places prefill legs. A live engine is configured and outside any ejection, hold or drain.
2. One other live engine that places prefill legs, choosing the engine with the fewest resident prefill requests and then by engine ID order. Prefill-role engines place prefill legs, and unpinned engines do when no prefill-role engine is live.
3. A standalone probe, which runs both legs on the held engine without a KV transfer.

A vLLM engine held without a recorded producer, such as one held for `inference_status` evidence alone, verifies through the standalone probe.

On SGLang, the probe pairs the held engine with a live peer in the other role:

- A held prefill engine runs the prefill leg for the live decode engines in turn, starting with the one that has the fewest resident decode requests and then by engine ID order. When no decode engine is live, or every one has failed its leg, the probe is `inconclusive` with the reason `no_consumer`.
- A held decode engine tries its recorded producer while it is live, and then one other live prefill-role engine, chosen as on vLLM. A held decode engine without a recorded producer, such as one held for `inference_status` evidence alone, tries only that other engine. With no prefill-role engine live, the probe is `inconclusive` with the reason `no_producer`. When every producer it tried has failed its leg, the engine stays held until the next probe.

When a peer leg fails, the verifier records `producer_failed` or `consumer_failed` and moves to the next peer. A peer leg that waits out the router's control pool makes the probe `inconclusive`, and the verifier keeps the hold and tries again at the next probe.

### Verification probe outcomes

`narwhal_engine_probes_total` counts verification probe outcomes by `iid`, `kind` and `outcome`:

| `outcome` | `kind` | Meaning |
| --- | --- | --- |
| `passed` | both | The health route answered, or both legs of one inference path passed. A health pass, or the pass of the last recorded path, clears the probed failure evidence when the profile-match rule holds. A profile mismatch ejects the engine with cause `profile_generation`. |
| `failed` | both | The health route returned an error or timed out, or a leg on the probed engine failed. A covered engine is ejected with cause `health_probe` or `inference_probe`. An uncovered engine stays in placement, and a failed inference probe also ends its hold with cause `uncovered`. |
| `inconclusive` | both | A probe leg waited out the router's control pool, a health-route timeout fired more than 1.5 times `engine.health_timeout_s` after its connection started, or on SGLang the probe had no peer to run with. The hold and streaks stay unchanged. |
| `producer_failed` | `verify_inference` | The producer's prefill leg failed. The next producer runs the probe. |
| `consumer_failed` | `verify_inference` | On SGLang, a decode engine's leg failed after the held prefill engine's leg passed. The next decode engine runs the probe. |
| `unavailable` | `verify_inference` | The fleet configuration names no model to probe with. The hold stays. |

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
| `liveness` | The health route failed on `recovery.liveness_misses` consecutive liveness sweeps. |
| `health_probe` | A health verification probe failed on a covered engine. |
| `inference_probe` | An inference verification probe failed on a covered engine. |
| `drift` | Decode drift stayed outside the engine's band on a covered engine. |
| `process_identity` | The engine's process identity changed or could not be read. |
| `profile_generation` | The loaded profiles do not match the live process generation. |
| `role_switch` | On SGLang with engine-side role switching, the engine's role switch failed. |

Hold-end `cause` values:

| `cause` | The hold ended because |
| --- | --- |
| `expired` | The timed quarantine deadline passed. |
| `superseded` | An inference hold replaced the timed quarantine. |
| `health` | A health-route answer met the profile-match rule. Inference holds ignore it. |
| `verification` | An inference probe passed every recorded path and met the profile-match rule. |
| `lifecycle` | Lifecycle readmission validated the engine. |
| `ejected` | The engine was ejected. |
| `drained` | An operator drain removed the engine from placement. |
| `uncovered` | No other live engine places the engine's roles. |

Readmission `evidence` values are `health`, `verification`, and `lifecycle`. An ejected [inference suspect](../concepts/03-Failure-and-State.md#failure-evidence) returns only on `verification` or `lifecycle`.

Each transition also writes a [journal event](01-Journal.md#journal-events): `engine_ejected`, `engine_hold_started`, `engine_hold_ended`, `engine_probe`, and `engine_readmitted`.
