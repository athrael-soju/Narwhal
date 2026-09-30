# Serving and role control

## 4. Request admission and bounded serving

By default, Narwhal dispatches each admitted request directly with one prefill and decode attempt.

### 4.1 Global admission

| Field                        | Default        | Meaning                                                                                                           |
| ---------------------------- | -------------- | ----------------------------------------------------------------------------------------------------------------- |
| `serving.admission`          | `"predictive"` | Admission mode. Accepted values are `predictive` and `open`.                                                      |
| `serving.admission_margin`   | `0.0`          | Fraction added to the TTFT admission budget. Nonnegative.                                                         |
| `serving.max_connections`    | `512`          | Global admitted-request limit and data connection pool size. At least 1.                                          |
| `engine.control_connections` | `0`            | Control connection pool size, reserved for health and recovery probes. `0` derives two per engine, with a minimum of four. Nonnegative. |

Admission modes:

| Mode         | Behavior                                                                                                                                                                                                                   |
| ------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `predictive` | Prices each prefill path against the time to first token (TTFT) target and limits aggregate placement to the measured single-phase region. Returns HTTP 429 when the least expensive prefill path exceeds the TTFT budget. |
| `open`       | Disables both admission checks.                                                                                                                                                                                            |

Backlog-driven refusals include `Retry-After` with the projected wait.

When a prompt's projected TTFT exceeds the target at zero backlog, Narwhal returns an error envelope that tells the caller to shorten the prompt or raise the TTFT target.

Measure sustained healthy inflight load before increasing `serving.max_connections`.

`narwhal-serve --max-concurrent` sets a lower admission limit for one router. `narwhal-serve` and Python router callers reject values above `serving.max_connections`.

### 4.2 Waiting, phase concurrency, and retries

Queueing requires positive values for `serving.queue_timeout_s`, `serving.prefill_concurrency`, `serving.decode_concurrency`, and `serving.handoff_timeout_s`.

| Field                         | Default    | Meaning                                                                                                                                                                            |
| ----------------------------- | ---------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `serving.queue_capacity`      | `0`        | Maximum requests waiting for admission. `0` rejects immediately at saturation.                                                                                                     |
| `serving.queue_timeout_s`     | `0.0`      | Maximum admission wait, capped by the original request deadline.                                                                                                                                                                                  |
| `serving.prefill_concurrency` | `0`        | Maximum resident prefill requests per engine while `serving.queue_capacity` is positive. Use a measured value.                                                                     |
| `serving.decode_concurrency`  | `0`        | Maximum resident decode requests per engine while `serving.queue_capacity` is positive. Use a measured value.                                                                      |
| `serving.handoff_timeout_s`   | `0.0`      | Maximum KV handoff age, counted from the start of the prefill HTTP request. Set it below the verified backend KV lease when queueing or retrying. At `0` the request deadline applies. |
| `serving.max_attempts`        | `1`        | Maximum complete prefill and decode attempts per original request. Range 1 to 3.                                                                                                   |
| `serving.retry_base_s`        | `0.1`      | Initial exponential-backoff ceiling. Full jitter samples from zero to this ceiling.                                                                                                |
| `serving.retry_cap_s`         | `1.0`      | Maximum backoff ceiling. At least the base value.                                                                                                                                  |
| `serving.retry_budget`        | `10`       | Initial and maximum retry-credit pool. Each retry spends one credit.                                                                                                               |
| `serving.retry_replenish`     | `0.1`      | Credits added after each successful original request. Range zero to one.                                                                                                           |
| `serving.max_request_bytes`   | `4194304`  | Maximum HTTP request-body size, enforced while reading.                                                                                                                            |
| `serving.max_response_bytes`  | `16777216` | Maximum retained bytes for each non-streaming attempt and the streaming pre-output metadata buffer.                                                                                |

Narwhal retains at most the admission limit plus `serving.queue_capacity` completion requests. The admission limit is `--max-concurrent` when set, otherwise `serving.max_connections`.

At this ceiling, a new completion request gets HTTP 429 before body parsing. Narwhal records it as an [unsized offer](../http-api/06-SLO-and-Demand.md#unsized-offers).

Queueing and phase-dispatch waits count toward demand. Retries keep the original arrival time, deadline, and reservation.

| Failure                                                    | Result                                                |
| ---------------------------------------------------------- | ----------------------------------------------------- |
| Transient transport error before visible output            | Retried when `serving.max_attempts` is greater than 1 |
| HTTP 408, 429, 500, 502, 503, or 504 before visible output | Retried when `serving.max_attempts` is greater than 1 |
| Permanent error                                            | Ends the request                                      |
| Local data connection pool starvation                      | Ends the request                                      |
| Cancellation                                               | Ends the request                                      |
| Any failure after visible output                           | Ends the request                                      |

Each retry, including recovery from an expired handoff, starts a complete prefill and decode attempt with a fresh KV handoff. Narwhal discards partial non-streaming response bodies from failed attempts before retrying.

The original request deadline covers:

- tokenization
- queue wait
- retry backoff
- engine work
- client writes

Set queue capacity, phase concurrency, and deadlines from measured workload latency and capacity. Repeat the measurement after changing queueing, concurrency limits, KV handoff expiry, retries, or byte limits.

Set `serving.handoff_timeout_s` below the producer's KV lease. Verify that the backend releases abandoned KV handoffs when the lease expires.

### 4.3 Streaming failure semantics

Streaming responses commit HTTP 200 before decode starts.

| Failure                                                                  | Client receives                         |
| ------------------------------------------------------------------------ | --------------------------------------- |
| Prefill failure                                                          | HTTP error                              |
| Non-streaming decode failure                                             | HTTP error                              |
| Streaming decode failure, including one before the first generated token | Terminal stream error event             |
| Request deadline expiry during a stream                                  | `code: expired`, and the stream closes  |
| Client backpressure                                                      | Immediate connection drop               |

Treat an error event, or a stream that ends before the success terminator, as a failed response.

Fit any client-side retry inside the caller's remaining deadline.

---

## 5. Placement

Placement filters candidate engines in this order:

1. Role
2. Availability
3. Exclusions
4. Projected service-level objective (SLO) compliance

Narwhal selects the lowest-cost eligible engine. Ties between equal-cost candidates break deterministically by instance ID.

If every candidate violates its projected SLO, Narwhal records an unserved placement and selects the lowest-cost fallback.

Engine-side prefix caching runs separately from router placement.

A live role change affects new placement immediately. Resident requests keep their current engine and reservation until completion or cancellation.

Lifecycle drains, quarantine, ejection, and restart holds persist across role changes.

Decode can fail after successful admission when KV handoffs expire before engines drain their work.

---

## 6. Request deadlines and engine HTTP behaviour

| Field                                 | Default  | Meaning                                                                                                                         |
| ------------------------------------- | -------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `profiles.path`                       | `"runs/profiles.json"` | Profile store read by the router and written by `narwhal-profile`.                                                                 |
| `serving.request_timeout_s`           | `600.0`  | End-to-end completion deadline from HTTP ingress through response delivery. Positive.                                           |
| `serving.prefill_timeout_s`           | `120.0`  | Elapsed prefill-leg deadline. Positive and at most `serving.request_timeout_s`.                                                 |
| `recovery.failure_quarantine_s`       | `0.0`    | Time a failed engine remains excluded from placement. `0` disables quarantine.                                                   |
| `engine.first_token_timeout_s`        | `2.5`    | Deadline to the first decode token and for each inference-probe leg. Positive and at most `serving.request_timeout_s`.          |
| `engine.first_token_calibration_path` | `""`     | Path to a completed first-token calibration artifact under `runs/`. An empty value leaves calibration evidence unconfigured.     |
| `engine.decode_read_timeout_s`        | `60.0`   | Maximum silent interval between decode chunks. `0` disables the gap limit.                                                      |
| `engine.tokenize`                     | `true`   | Requests exact text and chat input length from the dialect tokenization endpoint. Narwhal counts token-ID prompts locally.      |
| `engine.tokenize_timeout_s`           | `2.0`    | Elapsed exact-token-count deadline. Positive. Errors from an available tokenizer route fail the request before placement.       |
| `engine.chars_per_token`              | `3.8`    | Character-to-token fallback ratio. Positive.                                                                                    |
| `engine.connect_timeout_s`            | `10.0`   | TCP-connect deadline for engine requests. Positive.                                                                             |
| `engine.pool_timeout_s`               | `5.0`    | Maximum wait for a connection from the data or control connection pool. Positive.                                               |
| `engine.health_timeout_s`             | `5.0`    | HTTP I/O timeout for preflight, breaker, and readmission health probes. Positive.                                               |

### 6.1 Request and prefill deadlines

`serving.request_timeout_s` includes decode streaming. Set it from the supported output length and client deadline.

Set `serving.prefill_timeout_s` from measurements of the longest admitted inputs under the supported load.

For diagnostic profiling, `narwhal-profile --observation-timeout-s` sets the probe HTTP timeout.

Prefill, tokenization, and health calls each apply their phase timeout plus the connection and pool timeouts. The first budget to expire ends the call.

### 6.2 First-token deadline and calibration

The `engine.first_token_timeout_s` budget runs from before the decode HTTP stream opens until the first generated token arrives. It includes connection and response-header delays.

The inference probe applies `engine.first_token_timeout_s` independently to each complete leg, prefill and decode.

Configure the first-token deadline:

1. Run [first-token deadline calibration](../deploy/06-Profile-and-Preflight.md#calibrate-the-first-token-deadline).
2. Set `engine.first_token_timeout_s` above the candidate it prints.
3. Set `engine.first_token_calibration_path` to its artifact.

`narwhal-check` checks the artifact against the configured value and the live process generations.

| Calibration artifact  | Preflight      | Router startup |
| --------------------- | -------------- | -------------- |
| Empty path            | Logs a warning | Logs a warning |
| Stale or insufficient | Fails          | Blocked        |

### 6.3 Decode stream gaps

`engine.decode_read_timeout_s` applies after the first token. Partial server-sent events (SSE) lines and metadata chunks reset its timer.

Set this limit from measured inter-chunk gaps and the service's failure budget. At `0`, the request deadline alone bounds the stream after the first token.

Disable the gap limit:

```json
{
  "engine": {
    "decode_read_timeout_s": 0
  }
}
```

### 6.4 Token counting

| Input                                                                        | Token count                                                                   |
| ---------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| Completion prompt that is a nonempty, flat list of nonnegative integer token IDs | Local array length.                                                        |
| Text or chat input, with `engine.tokenize` set to `true` and a dialect exact-count route | That route, with deadline `engine.tokenize_timeout_s`. A failed call returns an engine error to the client. |
| Other text or chat input                                                     | Estimate from `engine.chars_per_token`.                                       |

`engine.chars_per_token` also feeds the quadratic prefill estimate. Measure it for the served tokenizer and for every dialect that uses this fallback.

### 6.5 Connection, pool, and health timeouts

Set `engine.connect_timeout_s` and `engine.pool_timeout_s` from connection setup and pool waits measured under the intended load.

When a health or inference probe waits longer than `engine.pool_timeout_s` for a control connection, the result is inconclusive. The engine keeps its current health verdict.

Set `engine.health_timeout_s` from health and identity latency measured under the intended load. A timed-out health probe counts as a failed liveness probe.

---

## 7. Role control

| Field                                       | Default | Meaning                                                                                                                                                              |
| ------------------------------------------- | ------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `controller.advisory`                       | `false` | Records proposed role splits and reasons while retaining the current roles.                                                                                        |
| `controller.monitor_interval_s`             | `1.0`   | Delay between engine monitoring passes. Positive.                                                                                                                    |
| `controller.monitor_failure_limit`          | `5`     | Consecutive passes with any engine monitoring stage failure before degraded state stops new admission. At least 1.                                                   |
| `controller.min_prefill`                    | `1`     | Minimum live prefill engines preserved by role-controller moves. At least 1.                                                                                         |
| `controller.min_decode`                     | `1`     | Minimum live decode engines preserved by role-controller moves. At least 1.                                                                                          |
| `controller.thresholds.expand`              | `1.0`   | SLO-relative pool load that starts reactive expansion. Positive.                                                                                                     |
| `controller.thresholds.shrink`              | `0.5`   | Maximum projected source load for ordinary consolidation. Nonnegative and lower than `expand`. `mixed_pressure` decode-to-prefill moves may exceed it.               |
| `controller.thresholds.cooldown_s`          | `10.0`  | Minimum time between prefill-to-decode moves. Nonnegative.                                                                                                           |
| `controller.thresholds.sustained_intervals` | `3`     | Confirmations required for moves that need them. Also the consecutive passes that arm the `panic_ratio` bypass. At least 1.                                          |
| `controller.thresholds.dwell_s`             | `0.0`   | Minimum residence time after an engine changes role. Nonnegative.                                                                                                    |
| `controller.thresholds.panic_ratio`         | `0.0`   | Multiple of `expand` that decode load must reach to skip the prefill-to-decode cooldown. Applies only when prefill load is at or below `shrink`. `0` disables it; otherwise at least 1. |
| `controller.thresholds.flip_resident_guard` | `0`     | Maximum resident decode streams allowed on a decode-to-prefill donor. `0` disables the guard.                                                                        |
| `controller.flip_history`                   | `1000`  | Maximum retained role-change records exposed by `/narwhal/state`. At least 1.                                                                                        |

`controller.monitor_interval_s` also bounds `recovery.health.min_samples` ([Breaker and drift settings](03-Recovery-and-Validation.md#81-breaker-and-drift-settings)). The maximum is:

```text
floor(recovery.health.window_s / controller.monitor_interval_s)
```

Each monitor pass contributes at most one residual per engine. Delayed passes can leave a window undersampled.

### 7.1 Load definitions

Prefill load is:

```text
predicted prefill work / TTFT target
```

| Term                                         | Definition                                                                                                                                             |
| -------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Decode correction                            | The engine's ratio of live to profiled decode latency, bounded by `controller.reactive.decode_correction_min` and `controller.reactive.decode_correction_max`. |
| Corrected idle floor                         | The profile's [zero-contention decode interval](../telemetry/02-Profiles.md#profile-fields), multiplied by the decode correction.                      |
| Remaining time per output token (TPOT) budget | The TPOT target minus the corrected idle floor.                                                                                                       |
| Observed token interval                      | The larger of the engine's mean token interval and its longest open inter-token gap.                                                                   |
| Decode load                                  | The observed token interval above the floor, divided by the remaining budget. Minimum 0. If the floor reaches the TPOT target, the raw interval-to-target ratio. |

A load of `1.0` means the phase has reached its target.

`/narwhal/state` fields and decision reasons call this quantity pressure, for example `mixed_pressure` and `source_pressure_safe`.

### 7.2 Role floors

The sum of `controller.min_prefill` and `controller.min_decode` must be at most the number of configured engines. A single engine can satisfy both floors at `1` and handles aggregate inference.

If pins, drains, quarantine, or health ejections leave too few movable engines for a floor, Narwhal reports the breach and keeps the safest reachable split.

Every role change preserves `controller.min_decode`.

When live decode capacity drops below its floor, engine monitoring restores one eligible engine per pass.

| Recovery      | Cooldown | Per-engine dwell |
| ------------- | -------- | ---------------- |
| Decode floor  | Skipped  | Applies          |
| Prefill floor | Skipped  | Skipped          |

Both floor recoveries:

- honor pins, availability, floor limits, and advisory mode
- record a dwell timestamp for each applied move
- count each applied move against `controller.flip_history`

Restored capacity returns the split to the adjacent-split decision path.

### 7.3 Resident work during role changes

A decode-to-prefill move requires:

- the new split and resident decode batches inside the profile domain
- KV capacity for the resident work

### 7.4 Advisory rollout

Run advisory mode against recorded traffic before allowing production role movement.

Enable advisory mode:

```json
{
  "controller": {
    "advisory": true
  }
}
```

`/narwhal/state` and Prometheus expose proposed prefill and decode counts, caller, reason, and result.

### 7.5 Adjacent-split decisions

The role controller compares the current split with each adjacent split. An adjacent split moves one engine between prefill and decode.

Ordinary consolidation requires both:

- projected source load at or below `controller.thresholds.shrink`
- reduction in the worst projected SLO ratio of at least `controller.reactive.movement_margin`

The `mixed_pressure` rule applies when both hold:

- measured prefill load reaches `controller.thresholds.expand`
- every engine has a profile

The rule can move one decode engine to prefill while projected decode load is above `shrink`. The move must improve the worst projected SLO ratio by at least the movement margin. At a zero margin, the improvement must be strictly positive.

One confirmation is enough in two cases:

- The move originates from a projected-TTFT recovery evaluation.
- All of these hold:
  - Demand evidence is complete.
  - The applicable rule differs from `mixed_pressure`.
  - The destination phase's current SLO ratio meets or exceeds `controller.thresholds.expand`.

Every other move needs:

```text
max(
  controller.reactive.confirmations,
  controller.thresholds.sustained_intervals
)
```

Changes that reset the confirmation sequence:

- proposed split
- demand completeness
- eligibility rule

A failed eligibility check clears the candidate.

Prefill-to-decode moves require prefill load at or below `shrink` and obey the prefill-to-decode cooldown.

Under overload, the movement margin, confirmations, consolidation evidence, and dwell limits stop repeated role changes. Persistent overload needs less offered demand or more capacity.

### 7.6 Evidence gating for decode-to-prefill consolidation

Decode-to-prefill consolidation depends on a closed arrival-evidence window and stable decode demand. The window closes on either condition:

- `controller.reactive.evidence_span_s` has elapsed with at least `controller.reactive.evidence_min_arrivals` samples
- `controller.reactive.evidence_max_span_s` has elapsed under sparse traffic

With enough short-horizon samples, consolidation pauses when:

```text
short_horizon_demand > long_horizon_demand * (1 + controller.reactive.demand_rise_tolerance)
```

Candidate pricing uses the larger demand estimate and includes resident plus pending decode work.

A first-token timeout or prefill-to-decode recovery move resets the consolidation evidence window.

Moves toward decode, including emergency floor restoration, can proceed while evidence accumulates. Predictive admission refusals during accumulation count toward both offered demand and attainment misses.

State and metrics expose:

- short and long demand estimates
- evidence-window state
- the gate blocking movement

### 7.7 Reactive-controller parameters

| Field                                               | Default | Meaning                                                                                                                                                                      |
| --------------------------------------------------- | ------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `controller.reactive.window_s`                      | `120.0` | Demand-estimation window. Positive.                                                                                                                                          |
| `controller.reactive.confirmations`                 | `2`     | Required consecutive identical adjacent proposals for moves that need confirmation. At least 1.                                                                             |
| `controller.reactive.utilization`                   | `0.8`   | Fraction of each engine treated as usable capacity. Greater than 0 and at most 1.                                                                                            |
| `controller.reactive.min_arrivals`                  | `10`    | Minimum arrival count required for a decision. At least 1.                                                                                                                   |
| `controller.reactive.demand_floor`                  | `0.5`   | Minimum accepted demand signal. Positive.                                                                                                                                    |
| `controller.reactive.movement_margin`               | `0.05`  | Required reduction in worst projected SLO ratio before movement. Range `[0, 1)`.                                                                                             |
| `controller.reactive.step_s`                        | `5.0`   | Minimum interval between scheduled adjacent-split evaluations. A projected prefill TTFT breach can trigger one projected-TTFT recovery evaluation between scheduled passes. Positive. |
| `controller.reactive.evidence_span_s`               | `60.0`  | Minimum recent-arrival span required for decode-to-prefill consolidation, and the short horizon for the rising-demand test. Positive and at most `evidence_max_span_s`.     |
| `controller.reactive.evidence_max_span_s`           | `120.0` | Maximum evidence duration under sparse traffic. Positive, at least `evidence_span_s`, and at most `window_s`.                                                                |
| `controller.reactive.evidence_min_arrivals`         | `10`    | Minimum samples within the evidence span before decode-to-prefill consolidation. At least 1.                                                                                 |
| `controller.reactive.demand_rise_tolerance`         | `0.25`  | Maximum accepted short-horizon rise over long-horizon decode demand. Finite and nonnegative.                                                                                 |
| `controller.reactive.decode_correction_min`         | `0.5`   | Lower bound on the live-to-profile decode correction. Positive.                                                                                                              |
| `controller.reactive.decode_correction_max`         | `2.0`   | Upper bound on the live-to-profile decode correction. At least the minimum.                                                                                                  |
| `controller.reactive.decode_correction_alpha`       | `0.2`   | Fraction of each qualifying observation window applied to the correction. Range `(0, 1]`.                                                                                    |
| `controller.reactive.decode_correction_min_samples` | `8`     | Required decode gaps before a window updates the correction. At least 1.                                                                                                     |

Demand pricing inputs:

- the mean profile across the configured engines, which share one hardware and tensor parallel (TP) shape
- active requests and resident KV tokens, as separate decode-profile inputs
- recent token intervals, which correct the estimate within bounds

Rerun preflight and deployment-load measurements after any engine build or serving policy change.
