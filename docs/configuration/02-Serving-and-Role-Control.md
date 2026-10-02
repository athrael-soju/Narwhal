---
description: Narwhal fleet settings for request admission, placement, deadlines and prefill and decode role control.
---

# Serving and role control

## 4. Request admission and bounded serving

### 4.1 Global admission

These fields set the admission mode and margin, the global admitted-request limit and the connection pool sizes.

| Field                        | Default        | Meaning                                                                | Values                                                 |
| ---------------------------- | -------------- | ---------------------------------------------------------------------- | ------------------------------------------------------ |
| `serving.admission`          | `"predictive"` | Admission mode.                                                        | `predictive` or `open`                                 |
| `serving.admission_margin`   | `0.0`          | Fraction of the TTFT target added to the admission budget.             | Zero or greater                                        |
| `serving.max_connections`    | `512`          | Global admitted-request limit and data connection pool size.           | At least 1                                             |
| `engine.control_connections` | `0`            | Control connection pool size, reserved for health and recovery probes. | `0` for `max(4, 2 × engine count)`, or a positive size |

In `predictive` mode, the router returns HTTP 429 when a request fails a time to first token (TTFT) or decode admission check. [Admission and refusal semantics](../http-api/02-Admission-and-Responses.md#admission-and-refusal-semantics) lists each predictive refusal with its HTTP 429 error and `Retry-After` value. `open` mode disables predictive refusals.

Measure sustained healthy inflight load before increasing `serving.max_connections`.

#### Decode admission check

The decode check projects the slot each request holds in the decode pool.

The check admits the request when the fleet has zero live decode engines, or when a live decode engine's profile or profiled `decode_max_requests` is unset.

The decode pool has one slot per request, up to the sum of `decode_max_requests`, each capped by `serving.decode_concurrency` when positive.

| Request                                    | Reaches decode                      | Holds a slot                                                                       |
| ------------------------------------------ | ----------------------------------- | ---------------------------------------------------------------------------------- |
| Request resident in decode                 |                                     | From now until its projected last token                                            |
| Request waiting for a decode slot          | Now                                 | From the earliest free slot until its projected last token                         |
| Request in prefill, and the checked request | At its predicted prefill completion | From the earliest free slot after it reaches decode until its projected last token |

Free slots go to requests in the order they reach decode.

| Term           | Meaning                                                                               |
| -------------- | ------------------------------------------------------------------------------------- |
| Slot wait      | Time from the checked request's predicted prefill completion to the start of its slot |
| Projected TTFT | Time since arrival plus the prefill placement price, including queueing               |
| Request KV     | Key-value (KV) tokens for the prompt plus delivered and remaining output              |

A request's output cap is `max_completion_tokens`, or `max_tokens` when `max_completion_tokens` is unset. Its bucket is its power-of-two prompt and output-cap sizes.

A capped request with three or more finished requests in its bucket expects its output cap times the bucket's median delivered fraction. A capped request with fewer finished requests in its bucket expects its output cap. An uncapped request expects the median delivered output for its prompt bucket, or the fleet-wide median delivered output.

Below the expected output, remaining output is the expected output minus delivered output. At or past the expected output, remaining output is the output cap minus delivered output, or unknown for an uncapped request.

Unknown remaining output sets these holds:

- the checked request holds its slot for the instant the slot starts
- every other request holds its slot indefinitely

A request resident in decode generates at its decode engine's token interval. Every other request generates at the fleet mean of the engine intervals. An engine's interval is its profiled token interval for a full batch at the current mean context, within the decode KV token bound, times the [decode correction](#71-load-definitions).

The check admits the request when all three checks pass:

| Check     | Passes when                                                                                                                                                  |
| --------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Slot wait | The slot wait is zero, or the projected TTFT plus the slot wait fits the [TTFT budget](../http-api/02-Admission-and-Responses.md#admission-and-refusal-semantics) |
| KV tokens | Peak request KV over the checked request's hold fits the sum of each engine's [decode KV token bound](../telemetry/02-Profiles.md#decode-capacity-derived-from-the-profile), or the request holds decode alone |
| TPOT      | A live decode engine meets `slo.tpot_s` with the request and the residents generating when its slot starts, or the request misses `slo.tpot_s` on every idle decode engine |

### 4.2 Waiting, phase concurrency, and retries

These fields set queueing, per-engine phase concurrency, KV handoff expiry, retry and byte limits.

| Field                         | Default    | Meaning                                                                                             | Values                                                                     |
| ----------------------------- | :--------: | --------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| `serving.queue_capacity`      | `0`        | Maximum requests waiting for admission.                                                             | `0` rejects immediately at saturation                                      |
| `serving.queue_timeout_s`     | `0.0`      | Maximum admission wait, capped by the original request deadline.                                    | Positive when `serving.queue_capacity` is positive                         |
| `serving.prefill_concurrency` | `0`        | Maximum resident prefill requests per engine.                                                       | Positive when `serving.queue_capacity` is positive                         |
| `serving.decode_concurrency`  | `0`        | Maximum resident decode requests per engine.                                                        | Positive when `serving.queue_capacity` is positive                         |
| `serving.handoff_timeout_s`   | `0.0`      | Maximum KV handoff age from the start of the prefill HTTP request, or the request deadline at `0`.  | Positive and below the verified backend KV lease when queueing or retrying |
| `serving.max_attempts`        | `1`        | Maximum complete prefill and decode attempts per original request.                                  | 1 to 3                                                                     |
| `serving.retry_base_s`        | `0.1`      | Initial exponential-backoff ceiling.                                                                | Positive                                                                   |
| `serving.retry_cap_s`         | `1.0`      | Maximum backoff ceiling.                                                                            | At least `serving.retry_base_s`                                            |
| `serving.retry_budget`        | `10`       | Initial and maximum retry-credit pool, at one credit per retry.                                     | Zero or greater                                                            |
| `serving.retry_replenish`     | `0.1`      | Credits added after each successful original request.                                               | 0 to 1                                                                     |
| `serving.max_request_bytes`   | `4194304`  | Maximum HTTP request-body size.                                                                     | At least 1                                                                 |
| `serving.max_response_bytes`  | `16777216` | Maximum retained bytes for each non-streaming attempt and the streaming pre-output metadata buffer. | At least 1                                                                 |

With a positive `serving.decode_concurrency`, role control prices each engine's decode capacity at that limit and floors each candidate split's decode load at:

```text
(decode residents on decode-role engines + requests waiting for a decode slot)
  / (serving.decode_concurrency * max(current decode engines, candidate decode engines))
```

The admission limit is [`narwhal-serve --max-concurrent`](06-Fabric-and-Operations.md#18-cli-precedence) when set, otherwise `serving.max_connections`. The router retains at most the admission limit plus `serving.queue_capacity` completion requests. A new request at that ceiling receives HTTP 429 before body parsing and counts as an [unsized offer](../http-api/06-SLO-and-Demand.md#unsized-offers).

With `serving.max_attempts` greater than 1, the router retries an attempt after a transient transport error, an HTTP 408, 429, 500, 502, 503 or 504 response, or an expired KV handoff, before visible output. A permanent error, local data connection pool starvation, cancellation, or any failure after visible output ends the request.

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

A streaming response holds its HTTP status and headers until the first output frame.

[Engine failure handling](../http-api/03-Backend-and-Failures.md#engine-failure-handling) gives the client response for each prefill and decode failure.

When the request deadline expires before the first output frame, the client receives HTTP 504. After the first output frame, the client receives a terminal stream error event with `code: expired`. While client writes are blocked, the router drops the connection immediately.

Treat an error event, or a stream that ends before the success terminator, as a failed response.

Fit client-side retries inside the caller's remaining deadline.

---

## 5. Placement

Placement selects the lowest-cost engine that passes the role, availability, exclusion and projected service-level objective (SLO) filters. Among equal-cost engines, it selects the lowest instance ID. When every candidate violates its projected SLO, placement selects the lowest-cost candidate as an unserved placement.

A live role change:

- applies to new placements immediately
- leaves resident requests on their current engine and reservation until completion or cancellation
- keeps lifecycle drains, quarantine, ejections, and restart holds in place

### 5.1 Prefix-cache pricing

For a request sized with exact token IDs, the router records each engine's cached leading prompt blocks from its [residency view](../http-api/05-Live-State.md#residency).

Each prefill placement rechecks those blocks against the engine's current view.

The router prices prefill on an engine that holds a cached prefix with that engine's [warm prefill fit](../measure/01-Profile.md#warm-prefill-with-a-cached-prefix).

Decisions that use the warm price:

- which engines meet the TTFT budget, and their placement order
- predictive admission
- resident work
- pool load
- offered demand
- role-split scoring

Offered demand takes warm prices from engines that run prefill: live prefill-role engines for the demand estimate, and each candidate split's prefill engines for role-split scoring.

The router prices a request on the cold curve of its full input in these cases:

- the router used its local length estimate
- the request carries multimodal content
- the request sets `truncate_prompt_tokens`, `documents`, or `reasoning_effort`
- the fleet config leaves `engine_contract` unset
- the fleet's `engine_contract` enables speculative decoding
- the engine's residency is unknown
- the engine's current view drops the prefix before placement
- the engine's profile holds a cold fit only
- the case lies outside the warm fit's measured domain

The request journal records each placement priced with cache evidence in [`cache_placement`](../telemetry/01-Journal.md#cache-placement).

---

## 6. Request deadlines and engine HTTP behaviour

These fields set request deadlines, token counting and the timeouts of engine HTTP requests.

| Field                                 | Default | Meaning                                                                           | Values                                        |
| ------------------------------------- | ------- | --------------------------------------------------------------------------------- | --------------------------------------------- |
| `serving.request_timeout_s`           | `600.0` | End-to-end completion deadline from HTTP ingress through response delivery.       | Positive                                      |
| `serving.prefill_timeout_s`           | `120.0` | Elapsed prefill-leg deadline.                                                     | Positive, at most `serving.request_timeout_s` |
| `engine.first_token_timeout_s`        | `2.5`   | Deadline to the first decode token.                                               | Positive, at most `serving.request_timeout_s` |
| `engine.first_token_calibration_path` | `""`    | Path to a completed first-token calibration artifact under `runs/`.               |                                               |
| `engine.decode_read_timeout_s`        | `60.0`  | Maximum silent interval between decode chunks after the first token.              | `0` disables the gap limit                    |
| `engine.tokenize`                     | `true`  | Requests exact text and chat input length from the dialect tokenization endpoint. |                                               |
| `engine.tokenize_timeout_s`           | `2.0`   | Elapsed exact-token-count deadline.                                               | Positive                                      |
| `engine.chars_per_token`              | `3.8`   | Character-to-token fallback ratio.                                                | Positive                                      |
| `engine.connect_timeout_s`            | `10.0`  | TCP-connect deadline for engine requests.                                         | Positive                                      |
| `engine.pool_timeout_s`               | `5.0`   | Maximum wait for a connection from the data or control connection pool.           | Positive                                      |
| `engine.health_timeout_s`             | `5.0`   | HTTP I/O timeout for preflight, breaker, readmission, and residency requests.     | Positive                                      |

### 6.1 Request and prefill deadlines

Set `serving.request_timeout_s` from the supported output length and the client deadline. Set `serving.prefill_timeout_s` from measurements of the longest admitted inputs under the supported load.

For diagnostic profiling, `narwhal-profile --observation-timeout-s` sets the probe HTTP timeout.

Prefill, tokenization, and health calls end when the first of their phase, connection, or pool timeouts expires.

### 6.2 First-token deadline and calibration

The `engine.first_token_timeout_s` window runs from before the decode HTTP stream opens to the first generated token.

Each inference probe leg, prefill and decode, has a budget of the larger of `engine.first_token_timeout_s` and `engine.health_timeout_s`.

Configure the first-token deadline:

1. Run [first-token deadline calibration](../deploy/06-Profile-and-Preflight.md#calibrating-the-first-token-deadline).
2. Set `engine.first_token_timeout_s` above the candidate it prints.
3. Set `engine.first_token_calibration_path` to its artifact.

The calibration artifact binds each engine to the same generation digest as its [saved profiles](../telemetry/02-Profiles.md#validating-the-engine-cost-model).

A change to an engine's generation digest makes the calibration artifact stale. An engine relaunch during calibration makes it insufficient.

With an empty `engine.first_token_calibration_path`, preflight and router startup log a warning. A stale or insufficient calibration artifact fails preflight and router startup.

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

The router counts a completion prompt that is a nonempty, flat list of nonnegative integer token IDs by its local array length.

For text or chat input, with `engine.tokenize` set to `true` and a dialect exact-count route, the router counts tokens through that route within `engine.tokenize_timeout_s`. When that exact-count call fails, the client receives the engine error before placement. Other text or chat input uses an estimate from `engine.chars_per_token`.

Measure `engine.chars_per_token` for the served tokenizer and for every dialect that uses this fallback.

### 6.5 Connection, pool, and health timeouts

Set these timeouts from latency measured under the intended load:

| Field                      | Measured latency             |
| -------------------------- | ---------------------------- |
| `engine.connect_timeout_s` | Connection setup             |
| `engine.pool_timeout_s`    | Pool waits                   |
| `engine.health_timeout_s`  | Health and identity requests |

A health or inference probe that waits longer than `engine.pool_timeout_s` for a control connection is inconclusive. A health probe that times out within 1.5 times `engine.health_timeout_s` of getting its control connection fails. A health probe that times out later is inconclusive.

An inconclusive probe has this effect for each caller:

| Caller                 | Inconclusive probe                      |
| ---------------------- | --------------------------------------- |
| Breaker verification   | Engine keeps its current health verdict |
| Liveness probe         | Engine keeps its current health verdict |
| Preflight reach        | Failed `/health` check                  |
| Readmission probe      | Engine stays ejected                    |
| Readmission validation | Failed `/health` check                  |

---

## 7. Role control

These fields configure the role controller, engine monitoring and the thresholds for role moves.

| Field                                       | Default | Meaning                                                                                               | Values                               |
| ------------------------------------------- | ------- | ----------------------------------------------------------------------------------------------------- | ------------------------------------ |
| `controller.advisory`                       | `false` | Holds current roles and records proposed role splits with reasons.                                    |                                      |
| `controller.monitor_interval_s`             | `1.0`   | Delay between engine monitoring passes and between residency refreshes.                               | Positive                             |
| `controller.monitor_failure_limit`          | `5`     | Consecutive passes with an engine monitoring stage failure before degraded state stops new admission. | At least 1                           |
| `controller.min_prefill`                    | `1`     | Minimum live prefill engines preserved by role-controller moves.                                      | At least 1                           |
| `controller.min_decode`                     | `1`     | Minimum live decode engines preserved by role-controller moves.                                       | At least 1                           |
| `controller.thresholds.expand`              | `1.0`   | SLO-relative pool load that starts reactive expansion.                                                | Positive                             |
| `controller.thresholds.shrink`              | `0.5`   | Maximum projected source load for ordinary consolidation.                                             | Zero or greater, lower than `expand` |
| `controller.thresholds.cooldown_s`          | `10.0`  | Minimum time between prefill-to-decode moves.                                                         | Zero or greater                      |
| `controller.thresholds.sustained_intervals` | `3`     | Confirmations required for moves that need them.                                                      | At least 1                           |
| `controller.thresholds.dwell_s`             | `0.0`   | Minimum residence time after an engine changes role.                                                  | Zero or greater                      |
| `controller.thresholds.panic_ratio`         | `0.0`   | Multiple of `expand` for the prefill-to-decode cooldown bypass.                                       | `0` for off, or at least 1           |
| `controller.thresholds.flip_resident_guard` | `0`     | Maximum resident decode streams allowed on a decode-to-prefill donor.                                 | `0` disables the guard               |
| `controller.flip_history`                   | `1000`  | Maximum retained role-change records exposed by `/narwhal/state`.                                     | At least 1                           |

### 7.1 Load definitions

Prefill load is:

```text
predicted prefill work / TTFT target
```

Decode load is:

```text
max(0, observed token interval - corrected idle floor) / remaining TPOT budget
```

The observed token interval is the larger of the engine's mean token interval and its longest open inter-token gap. The corrected idle floor is the profile's [zero-contention decode interval](../telemetry/02-Profiles.md#profile-fields), multiplied by the decode correction. The remaining time per output token (TPOT) budget is the TPOT target minus the corrected idle floor.

The decode correction is the engine's ratio of live to profiled decode latency, bounded by `controller.reactive.decode_correction_min` and `controller.reactive.decode_correction_max`. When the corrected idle floor reaches the TPOT target, decode load is the raw interval-to-target ratio.

A load of `1.0` means the phase has reached its target.

Controller decision details name load as pressure, such as the `mixed_pressure` eligibility rule and the `source_pressure_safe` flag.

### 7.2 Role floors

In a fleet of two or more engines, `controller.min_prefill` plus `controller.min_decode` is at most the engine count. A one-engine fleet has both floors at `1`.

When pins, drains, quarantine, or health ejections leave too few movable engines for a floor, Narwhal reports a floor breach.

Every role change preserves `controller.min_decode`.

When live decode capacity drops below its floor, engine monitoring restores one eligible engine per pass.

Decode floor recovery skips the cooldown and applies the per-engine dwell. Prefill floor recovery skips both.

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

The worst projected SLO ratio of a split is the largest of its projected TTFT, TPOT and decode queueing ratios.

| Move              | Projected source load                                            |
| ----------------- | ---------------------------------------------------------------- |
| Prefill-to-decode | The candidate's projected TTFT ratio                             |
| Decode-to-prefill | The larger of the candidate's TPOT and decode queueing ratios    |

The decode queueing ratio projects the slots of the [decode admission check](#decode-admission-check) onto the split's decode engines:

```text
max over waiting decode requests and requests in prefill:
  slot wait / (slo.ttft_s - time since arrival - time until the request reaches decode)
```

The split's decode pool has the live mean decode slots per engine times its decode engine count.

A request at or past its TTFT deadline when it reaches decode contributes zero.

Adjacent-split decisions use these spans:

| Span              | Length                                                                                                                   | Default |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------ | :-----: |
| Window            | `controller.reactive.window_s`                                                                                           |  120 s  |
| Settled run       | `controller.reactive.evidence_span_s`                                                                                    |   60 s  |
| Confirmation span | `controller.reactive.step_s` times max(`controller.reactive.confirmations`, `controller.thresholds.sustained_intervals`) |   15 s  |

Each scored decision names one rule in `eligibility_rule`, the first match in this order:

1. `projected_ttft_recovery`
2. `mixed_pressure`
3. `settled_departure`
4. `steady_demand`
5. `source_shrink`

Ordinary consolidation, the `source_shrink` rule, requires both:

- projected source load at or below `controller.thresholds.shrink`
- reduction in the worst projected SLO ratio of at least `controller.reactive.movement_margin`

The `mixed_pressure` rule moves one decode engine to prefill when projected decode load is above `shrink` and all of these hold:

- measured prefill load reaches `controller.thresholds.expand`
- every engine has a profile
- the move improves the worst projected SLO ratio by a positive amount of at least `controller.reactive.movement_margin`

The role controller opens a departure when all of these hold:

- for the settled run, every adjacent split has improved the worst projected SLO ratio by less than `controller.reactive.movement_margin` on window demand and on confirmation-span demand
- confirmation-span demand for either phase differs from window demand by more than `controller.reactive.demand_rise_tolerance` times the larger estimate
- an adjacent split improves the confirmation-span worst projected SLO ratio by at least `controller.reactive.movement_margin`

The `settled_departure` rule moves one engine toward that split when both hold on confirmation-span demand:

- projected source load at or below `controller.thresholds.shrink`
- reduction in the worst projected SLO ratio of at least `controller.reactive.movement_margin`

Moves that follow the departure's move use window demand.

The departure holds the reverse move until `controller.reactive.window_s` has elapsed since it opened, or until a window-demand move continues in the departure's direction.

When `mixed_pressure` applies to the departure's split on window demand, that split keeps window demand and the `mixed_pressure` rule.

A departure closes before its move when the best confirmation-span adjacent split changes direction or improves the worst projected SLO ratio by less than `controller.reactive.movement_margin`.

Demand is steady when both hold:

- for `controller.reactive.evidence_span_s`, confirmation-span demand and window demand for each phase have differed by at most `controller.reactive.demand_rise_tolerance` times the larger estimate
- the arrival-evidence window is closed

The `steady_demand` rule moves one engine under steady demand when both hold:

- projected source load above `controller.thresholds.shrink` and at or below `controller.thresholds.expand`
- reduction in the worst projected SLO ratio of at least `controller.reactive.movement_margin`

Projected-TTFT recovery evaluations leave the settled run, `steady_demand_s` and an open departure unchanged.

A standby router or a lifecycle hold pauses role control and restarts the settled run and `steady_demand_s`.

One confirmation is enough in three cases:

- The move originates from a projected-TTFT recovery evaluation.
- The eligibility rule is `settled_departure`.
- All of these hold:
  - Demand evidence is complete.
  - The eligibility rule is `source_shrink`.
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

- projected prefill load at or below `controller.thresholds.shrink`, or at or below `controller.thresholds.expand` under `steady_demand`
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

The `source_shrink` and `settled_departure` rules move engines toward decode while evidence accumulates.

The `steady_demand` rule requires a closed evidence window in both directions.

State and metrics expose:

- short and long demand estimates
- evidence-window state
- the gate blocking movement

### 7.7 Reactive-controller parameters

These fields tune demand estimation, move confirmation, the arrival-evidence window and the decode correction.

| Field                                               | Default | Meaning                                                                                                                                              | Values                                                   |
| --------------------------------------------------- | :-----: | ---------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------- |
| `controller.reactive.window_s`                      | `120.0` | Demand-estimation window and the length of a departure's reverse-move hold.                                                                          | Positive                                                 |
| `controller.reactive.confirmations`                 | `2`     | Required consecutive identical adjacent proposals for moves that need confirmation, and a factor of the confirmation span.                           | At least 1                                               |
| `controller.reactive.utilization`                   | `0.8`   | Fraction of each engine treated as usable capacity.                                                                                                  | Greater than 0, at most 1                                |
| `controller.reactive.min_arrivals`                  | `10`    | Minimum arrival count required for a decision.                                                                                                       | At least 1                                               |
| `controller.reactive.demand_floor`                  | `0.5`   | Minimum accepted demand signal.                                                                                                                      | Positive                                                 |
| `controller.reactive.movement_margin`               | `0.05`  | Required reduction in worst projected SLO ratio before movement.                                                                                     | `[0, 1)`                                                 |
| `controller.reactive.step_s`                        | `5.0`   | Minimum interval between scheduled adjacent-split evaluations and the unit of the confirmation span.                                                 | Positive                                                 |
| `controller.reactive.evidence_span_s`               | `60.0`  | Minimum recent-arrival span for decode-to-prefill consolidation, the duration of matching demand that makes demand steady, and the settled-run length. | Positive, at most `evidence_max_span_s`                  |
| `controller.reactive.evidence_max_span_s`           | `120.0` | Maximum evidence duration under sparse traffic.                                                                                                      | Positive, at least `evidence_span_s`, at most `window_s` |
| `controller.reactive.evidence_min_arrivals`         | `10`    | Minimum samples within the evidence span before decode-to-prefill consolidation.                                                                     | At least 1                                               |
| `controller.reactive.demand_rise_tolerance`         | `0.25`  | Maximum accepted short-horizon rise over long-horizon decode demand, and the fraction of the larger estimate within which two demand horizons match. | Zero or greater                                          |
| `controller.reactive.decode_correction_min`         | `0.5`   | Lower bound on the live-to-profile decode correction.                                                                                                | Positive                                                 |
| `controller.reactive.decode_correction_max`         | `2.0`   | Upper bound on the live-to-profile decode correction.                                                                                                | At least `decode_correction_min`                         |
| `controller.reactive.decode_correction_alpha`       | `0.2`   | Fraction of each qualifying observation window applied to the correction.                                                                            | `(0, 1]`                                                 |
| `controller.reactive.decode_correction_min_samples` | `8`     | Required decode gaps before a window updates the correction.                                                                                         | At least 1                                               |
