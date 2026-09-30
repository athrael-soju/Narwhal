# Serving and role control

## 4. Request admission and bounded serving

### 4.1 Global admission

| Field                        | Default        | Meaning                                                                | Values                                                            |
| ---------------------------- | -------------- | ---------------------------------------------------------------------- | ----------------------------------------------------------------- |
| `serving.admission`          | `"predictive"` | Admission mode.                                                        | `predictive` or `open`                                            |
| `serving.admission_margin`   | `0.0`          | Fraction added to the TTFT admission budget.                           | Zero or greater                                                       |
| `serving.max_connections`    | `512`          | Global admitted-request limit and data connection pool size.           | At least 1                                                        |
| `engine.control_connections` | `0`            | Control connection pool size, reserved for health and recovery probes. | `0` for two per engine with a minimum of four, or a positive size |

Admission modes:

| Mode         | Behavior                                                                                              |
| ------------ | ----------------------------------------------------------------------------------------------------- |
| `predictive` | Returns HTTP 429 when the least expensive prefill path exceeds the time to first token (TTFT) budget. |
| `open`       | Disables predictive refusals.                                                                         |

| Refusal                                         | Response                                                                            |
| ----------------------------------------------- | ----------------------------------------------------------------------------------- |
| Backlog-driven                                  | `Retry-After` header with the projected wait                                        |
| Projected TTFT above the target at zero backlog | Error envelope that tells the caller to shorten the prompt or raise the TTFT target |

Measure sustained healthy inflight load before increasing `serving.max_connections`.

`narwhal-serve --max-concurrent` sets a per-router admission limit of at most `serving.max_connections`.

### 4.2 Waiting, phase concurrency, and retries

| Field                         | Default    | Meaning                                                                                             | Values                                                                     |
| ----------------------------- | ---------- | --------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| `serving.queue_capacity`      | `0`        | Maximum requests waiting for admission.                                                             | `0` rejects immediately at saturation                                      |
| `serving.queue_timeout_s`     | `0.0`      | Maximum admission wait, capped by the original request deadline.                                    | Positive when `serving.queue_capacity` is positive                         |
| `serving.prefill_concurrency` | `0`        | Maximum resident prefill requests per engine.                                                       | Positive when `serving.queue_capacity` is positive                         |
| `serving.decode_concurrency`  | `0`        | Maximum resident decode requests per engine.                                                        | Positive when `serving.queue_capacity` is positive                         |
| `serving.handoff_timeout_s`   | `0.0`      | Maximum KV handoff age from the start of the prefill HTTP request, or the request deadline at `0`.  | Positive and below the verified backend KV lease when queueing or retrying |
| `serving.max_attempts`        | `1`        | Maximum complete prefill and decode attempts per original request.                                  | 1 to 3                                                                     |
| `serving.retry_base_s`        | `0.1`      | Initial exponential-backoff ceiling.                                                                |                                                                            |
| `serving.retry_cap_s`         | `1.0`      | Maximum backoff ceiling.                                                                            | At least `serving.retry_base_s`                                            |
| `serving.retry_budget`        | `10`       | Initial and maximum retry-credit pool, at one credit per retry.                                     |                                                                            |
| `serving.retry_replenish`     | `0.1`      | Credits added after each successful original request.                                               | 0 to 1                                                                     |
| `serving.max_request_bytes`   | `4194304`  | Maximum HTTP request-body size.                                                                     |                                                                            |
| `serving.max_response_bytes`  | `16777216` | Maximum retained bytes for each non-streaming attempt and the streaming pre-output metadata buffer. |                                                                            |

Retained completion requests:

- The admission limit is `--max-concurrent` when set, otherwise `serving.max_connections`.
- The retained-request ceiling is the admission limit plus `serving.queue_capacity` completion requests.
- A new completion request at that ceiling gets HTTP 429 before body parsing.
- Each refusal counts as an [unsized offer](../http-api/06-SLO-and-Demand.md#unsized-offers).

| Failure                                                    | Result                                                |
| ---------------------------------------------------------- | ----------------------------------------------------- |
| Transient transport error before visible output            | Retried when `serving.max_attempts` is greater than 1 |
| HTTP 408, 429, 500, 502, 503, or 504 before visible output | Retried when `serving.max_attempts` is greater than 1 |
| Expired KV handoff                                         | Retried when `serving.max_attempts` is greater than 1 |
| Permanent error                                            | Ends the request                                      |
| Local data connection pool starvation                      | Ends the request                                      |
| Cancellation                                               | Ends the request                                      |
| Any failure after visible output                           | Ends the request                                      |

The original request deadline covers:

- tokenization
- queue wait
- retry backoff
- engine work
- client writes

Tune queueing and retries:

1. Set queue capacity, phase concurrency, and deadlines from measured workload latency and capacity.
2. Verify that the backend releases abandoned KV handoffs when the lease expires.
3. Repeat the measurement after changing queueing, concurrency limits, KV handoff expiry, retries, or byte limits.

### 4.3 Streaming failure semantics

Streaming responses commit HTTP 200 before decode starts.

| Failure                                                                  | Client receives                                  |
| ------------------------------------------------------------------------ | ------------------------------------------------ |
| Prefill failure                                                          | HTTP error                                       |
| Non-streaming decode failure                                             | HTTP error                                       |
| Streaming decode failure, including one before the first generated token | Terminal stream error event                      |
| Request deadline expiry during a stream                                  | Terminal stream error event with `code: expired` |
| Client backpressure                                                      | Immediate connection drop                        |

Treat an error event, or a stream that ends before the success terminator, as a failed response.

Fit client-side retries inside the caller's remaining deadline.

## 5. Placement

| Case                                                                                                | Selected engine                                |
| --------------------------------------------------------------------------------------------------- | ---------------------------------------------- |
| Engines pass the role, availability, exclusion, and projected service-level objective (SLO) filters | Lowest-cost passing engine                     |
| Equal-cost engines                                                                                  | Lowest instance ID                             |
| Every candidate violates its projected SLO                                                          | Lowest-cost candidate as an unserved placement |

A live role change:

- applies to new placements immediately
- leaves resident requests on their current engine and reservation until completion or cancellation
- keeps lifecycle drains, quarantine, ejections, and restart holds in place

## 6. Request deadlines and engine HTTP behaviour

| Field                                 | Default                | Meaning                                                                           | Values                                        |
| ------------------------------------- | ---------------------- | --------------------------------------------------------------------------------- | --------------------------------------------- |
| `profiles.path`                       | `"runs/profiles.json"` | Profile store read by the router and written by `narwhal-profile`.                |                                               |
| `serving.request_timeout_s`           | `600.0`                | End-to-end completion deadline from HTTP ingress through response delivery.       | Positive                                      |
| `serving.prefill_timeout_s`           | `120.0`                | Elapsed prefill-leg deadline.                                                     | Positive, at most `serving.request_timeout_s` |
| `recovery.failure_quarantine_s`       | `0.0`                  | Time a failed engine remains excluded from placement.                             | `0` disables quarantine                       |
| `engine.first_token_timeout_s`        | `2.5`                  | Deadline to the first decode token.                                               | Positive, at most `serving.request_timeout_s` |
| `engine.first_token_calibration_path` | `""`                   | Path to a completed first-token calibration artifact under `runs/`.               |                                               |
| `engine.decode_read_timeout_s`        | `60.0`                 | Maximum silent interval between decode chunks after the first token.             | `0` disables the gap limit                    |
| `engine.tokenize`                     | `true`                 | Requests exact text and chat input length from the dialect tokenization endpoint. |                                               |
| `engine.tokenize_timeout_s`           | `2.0`                  | Elapsed exact-token-count deadline.                                               | Positive                                      |
| `engine.chars_per_token`              | `3.8`                  | Character-to-token fallback ratio.                                                | Positive                                      |
| `engine.connect_timeout_s`            | `10.0`                 | TCP-connect deadline for engine requests.                                         | Positive                                      |
| `engine.pool_timeout_s`               | `5.0`                  | Maximum wait for a connection from the data or control connection pool.           | Positive                                      |
| `engine.health_timeout_s`             | `5.0`                  | HTTP I/O timeout for preflight, breaker, and readmission health probes.           | Positive                                      |

### 6.1 Request and prefill deadlines

Set `serving.request_timeout_s` from the supported output length and client deadline.

Set `serving.prefill_timeout_s` from measurements of the longest admitted inputs under the supported load.

For diagnostic profiling, `narwhal-profile --observation-timeout-s` sets the probe HTTP timeout.

Prefill, tokenization, and health calls end when the first of their phase, connection, or pool timeouts expires.

### 6.2 First-token deadline and calibration

The `engine.first_token_timeout_s` window starts before the decode HTTP stream opens and ends at the first generated token.

The inference probe applies `engine.first_token_timeout_s` independently to each complete leg, prefill and decode.

Configure the first-token deadline:

1. Run [first-token deadline calibration](../deploy/06-Profile-and-Preflight.md#calibrate-the-first-token-deadline).
2. Set `engine.first_token_timeout_s` above the candidate it prints.
3. Set `engine.first_token_calibration_path` to its artifact.

An engine process restart makes the calibration artifact stale.

| Calibration artifact  | Preflight      | Router startup |
| --------------------- | -------------- | -------------- |
| Empty path            | Logs a warning | Logs a warning |
| Stale or insufficient | Fails          | Blocked        |

### 6.3 Decode stream gaps

Set `engine.decode_read_timeout_s` from measured inter-chunk gaps and the service's failure budget.

Disable the gap limit:

```json
{
  "engine": {
    "decode_read_timeout_s": 0
  }
}
```

### 6.4 Token counting

| Input                                                                                    | Token count                                     |
| ---------------------------------------------------------------------------------------- | ----------------------------------------------- |
| Completion prompt that is a nonempty, flat list of nonnegative integer token IDs         | Local array length                            |
| Text or chat input, with `engine.tokenize` set to `true` and a dialect exact-count route | That route, within `engine.tokenize_timeout_s` |
| Text or chat input whose exact-count call fails                                          | Engine error to the client before placement    |
| Other text or chat input                                                                 | Estimate from `engine.chars_per_token`         |

Measure `engine.chars_per_token` for the served tokenizer and for every dialect that uses this fallback.

### 6.5 Connection, pool, and health timeouts

Set these timeouts from latency measured under the intended load:

| Field                      | Measured latency             |
| -------------------------- | ---------------------------- |
| `engine.connect_timeout_s` | Connection setup             |
| `engine.pool_timeout_s`    | Pool waits                   |
| `engine.health_timeout_s`  | Health and identity requests |

| Timeout                                                                                      | Result                                  |
| -------------------------------------------------------------------------------------------- | --------------------------------------- |
| Health or inference probe waits longer than `engine.pool_timeout_s` for a control connection | Engine keeps its current health verdict |
| Health probe exceeds `engine.health_timeout_s`                                               | Failed liveness probe                   |

## 7. Role control

| Field                                       | Default | Meaning                                                                                               | Values                           |
| ------------------------------------------- | ------- | ----------------------------------------------------------------------------------------------------- | -------------------------------- |
| `controller.advisory`                       | `false` | Holds current roles and records proposed role splits with reasons.                           |                                  |
| `controller.monitor_interval_s`             | `1.0`   | Delay between engine monitoring passes.                                                               | Positive                         |
| `controller.monitor_failure_limit`          | `5`     | Consecutive passes with an engine monitoring stage failure before degraded state stops new admission. | At least 1                       |
| `controller.min_prefill`                    | `1`     | Minimum live prefill engines preserved by role-controller moves.                                      | At least 1                       |
| `controller.min_decode`                     | `1`     | Minimum live decode engines preserved by role-controller moves.                                       | At least 1                       |
| `controller.thresholds.expand`              | `1.0`   | SLO-relative pool load that starts reactive expansion.                                                | Positive                         |
| `controller.thresholds.shrink`              | `0.5`   | Maximum projected source load for ordinary consolidation.                                             | Zero or greater, lower than `expand` |
| `controller.thresholds.cooldown_s`          | `10.0`  | Minimum time between prefill-to-decode moves.                                                         | Zero or greater                      |
| `controller.thresholds.sustained_intervals` | `3`     | Confirmations required for moves that need them.                                                      | At least 1                       |
| `controller.thresholds.dwell_s`             | `0.0`   | Minimum residence time after an engine changes role.                                                  | Zero or greater                      |
| `controller.thresholds.panic_ratio`         | `0.0`   | Multiple of `expand` for the prefill-to-decode cooldown bypass.                                       | `0` for off, or at least 1       |
| `controller.thresholds.flip_resident_guard` | `0`     | Maximum resident decode streams allowed on a decode-to-prefill donor.                                 | `0` disables the guard           |
| `controller.flip_history`                   | `1000`  | Maximum retained role-change records exposed by `/narwhal/state`.                                     | At least 1                       |

The maximum `recovery.health.min_samples` in [breaker and drift settings](03-Recovery-and-Validation.md#81-breaker-and-drift-settings) is:

```text
floor(recovery.health.window_s / controller.monitor_interval_s)
```

### 7.1 Load definitions

Prefill load is:

```text
predicted prefill work / TTFT target
```

| Term                                                | Definition                                                                                                                                                     |
| --------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Decode correction                                   | The engine's ratio of live to profiled decode latency, bounded by `controller.reactive.decode_correction_min` and `controller.reactive.decode_correction_max`. |
| Corrected idle floor                                | The profile's [zero-contention decode interval](../telemetry/02-Profiles.md#profile-fields), multiplied by the decode correction.                              |
| Remaining time per output token (TPOT) budget       | The TPOT target minus the corrected idle floor.                                                                                                                |
| Observed token interval                             | The larger of the engine's mean token interval and its longest open inter-token gap.                                                                           |
| Decode load                                         | The observed token interval above the floor divided by the remaining budget, at least 0.                                                                       |
| Decode load at a floor that reaches the TPOT target | The raw interval-to-target ratio.                                                                                                                              |

A load of `1.0` means the phase has reached its target.

`/narwhal/state` fields and decision reasons call load pressure, for example `mixed_pressure` and `source_pressure_safe`.

### 7.2 Role floors

| Fleet               | Floor rule                                                                     |
| ------------------- | ------------------------------------------------------------------------------ |
| Two or more engines | `controller.min_prefill` plus `controller.min_decode` at most the engine count |
| One engine          | Both floors at `1`                                    |

When pins, drains, quarantine, or health ejections leave too few movable engines for a floor, Narwhal reports a floor breach.

Every role change preserves `controller.min_decode`.

When live decode capacity drops below its floor, engine monitoring restores one eligible engine per pass.

| Recovery      | Cooldown | Per-engine dwell |
| ------------- | -------- | ---------------- |
| Decode floor  | Skipped  | Applies          |
| Prefill floor | Skipped  | Skipped          |

Both floor recoveries:

- honor pins, availability, floor limits, and advisory mode
- count each applied move against `controller.flip_history`

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

An adjacent split moves one engine between prefill and decode.

Ordinary consolidation requires both:

- projected source load at or below `controller.thresholds.shrink`
- reduction in the worst projected SLO ratio of at least `controller.reactive.movement_margin`

The `mixed_pressure` rule moves one decode engine to prefill when projected decode load is above `shrink` and all of these hold:

- measured prefill load reaches `controller.thresholds.expand`
- every engine has a profile
- the move improves the worst projected SLO ratio by a positive amount of at least `controller.reactive.movement_margin`

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

These reset the confirmation sequence:

- a change in the proposed split
- a change in demand completeness
- a change in the eligibility rule
- a failed eligibility check

Prefill-to-decode moves require:

- prefill load at or below `controller.thresholds.shrink`
- an elapsed `controller.thresholds.cooldown_s`, or an armed cooldown bypass

The cooldown bypass arms after `controller.thresholds.sustained_intervals` consecutive passes with both:

- decode load at or above `controller.thresholds.panic_ratio` times `controller.thresholds.expand`
- prefill load at or below `controller.thresholds.shrink`

### 7.6 Evidence gating for decode-to-prefill consolidation

Decode-to-prefill consolidation waits for the arrival-evidence window to close on either condition:

- `controller.reactive.evidence_span_s` has elapsed with at least `controller.reactive.evidence_min_arrivals` samples
- `controller.reactive.evidence_max_span_s` has elapsed under sparse traffic

With enough samples in the short horizon of `controller.reactive.evidence_span_s`, consolidation pauses when:

```text
short_horizon_demand > long_horizon_demand * (1 + controller.reactive.demand_rise_tolerance)
```

A first-token timeout or prefill-to-decode recovery move resets the consolidation evidence window.

Moves toward decode can proceed while evidence accumulates.

State and metrics expose:

- short and long demand estimates
- evidence-window state
- the gate blocking movement

### 7.7 Reactive-controller parameters

| Field                                               | Default | Meaning                                                                             | Values                                                   |
| --------------------------------------------------- | ------- | ----------------------------------------------------------------------------------- | -------------------------------------------------------- |
| `controller.reactive.window_s`                      | `120.0` | Demand-estimation window.                                                           | Positive                                                 |
| `controller.reactive.confirmations`                 | `2`     | Required consecutive identical adjacent proposals for moves that need confirmation. | At least 1                                               |
| `controller.reactive.utilization`                   | `0.8`   | Fraction of each engine treated as usable capacity.                                 | Greater than 0, at most 1                                |
| `controller.reactive.min_arrivals`                  | `10`    | Minimum arrival count required for a decision.                                      | At least 1                                               |
| `controller.reactive.demand_floor`                  | `0.5`   | Minimum accepted demand signal.                                                     | Positive                                                 |
| `controller.reactive.movement_margin`               | `0.05`  | Required reduction in worst projected SLO ratio before movement.                    | `[0, 1)`                                                 |
| `controller.reactive.step_s`                        | `5.0`   | Minimum interval between scheduled adjacent-split evaluations.                      | Positive                                                 |
| `controller.reactive.evidence_span_s`               | `60.0`  | Minimum recent-arrival span required for decode-to-prefill consolidation.           | Positive, at most `evidence_max_span_s`                  |
| `controller.reactive.evidence_max_span_s`           | `120.0` | Maximum evidence duration under sparse traffic.                                     | Positive, at least `evidence_span_s`, at most `window_s` |
| `controller.reactive.evidence_min_arrivals`         | `10`    | Minimum samples within the evidence span before decode-to-prefill consolidation.    | At least 1                                               |
| `controller.reactive.demand_rise_tolerance`         | `0.25`  | Maximum accepted short-horizon rise over long-horizon decode demand.                | Finite, zero or greater                                      |
| `controller.reactive.decode_correction_min`         | `0.5`   | Lower bound on the live-to-profile decode correction.                               | Positive                                                 |
| `controller.reactive.decode_correction_max`         | `2.0`   | Upper bound on the live-to-profile decode correction.                               | At least `decode_correction_min`                         |
| `controller.reactive.decode_correction_alpha`       | `0.2`   | Fraction of each qualifying observation window applied to the correction.           | `(0, 1]`                                                 |
| `controller.reactive.decode_correction_min_samples` | `8`     | Required decode gaps before a window updates the correction.                        | At least 1                                               |

Rerun preflight and deployment-load measurements after an engine build or serving policy change.
