---
description: Narwhal fleet settings for request admission, placement, deadlines and prefill and decode role control.
---

# Serving and role control

## 4. Request admission and bounded serving

### 4.1 Global admission

| Field                        | Default        | Meaning                                                                | Values                                                 |
| ---------------------------- | -------------- | ---------------------------------------------------------------------- | ------------------------------------------------------ |
| `serving.admission`          | `"predictive"` | Admission mode.                                                        | `predictive` or `open`                                 |
| `serving.admission_margin`   | `0.0`          | Fraction of the TTFT target added to the admission budget.             | Zero or greater                                        |
| `serving.max_connections`    | `512`          | Global admitted-request limit and data connection pool size.           | At least 1                                             |
| `engine.control_connections` | `0`            | Control connection pool size, reserved for health and recovery probes. | `0` for `max(4, 2 × engine count)`, or a positive size |

Admission modes:

| Mode         | Behavior                                                                                      |
| ------------ | --------------------------------------------------------------------------------------------- |
| `predictive` | Returns HTTP 429 when a request fails a time to first token (TTFT) or decode admission check. |
| `open`       | Disables predictive refusals.                                                                 |

| Refusal                                                                                  | Response                                                                            |
| ---------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| Backlog-driven                                                                           | `Retry-After` header with the projected wait beyond the budget                      |
| Prefill of the prompt alone exceeds the TTFT budget                                      | Error envelope that tells the caller to shorten the prompt or raise the TTFT budget |
| Aggregate prefill while every live engine carries decode work                            | `Retry-After: 1`                                                                    |
| Peak projected decode work over the request's decode window exceeds live decode capacity | `Retry-After: 1`                                                                    |
| Decode load pushes the request past `slo.tpot_s` on every live decode engine             | `Retry-After: 1`                                                                    |

Measure sustained healthy inflight load before increasing `serving.max_connections`.

#### Decode admission check

The decode check covers the request's decode window, from its predicted prefill completion to its projected last token.

The decode check admits the request outright in either case:

- zero live decode engines
- a live decode engine whose profile or profiled `decode_max_requests` is unset

| Request                           | Holds decode                                                         |
| --------------------------------- | -------------------------------------------------------------------- |
| The checked request               | From its predicted prefill completion                                |
| Request waiting for a decode slot | From now through the whole window                                    |
| Request resident in decode        | From now until its projected last token                              |
| Request in prefill                | From its predicted prefill completion until its projected last token |

| Delivered output                                   | Remaining output                       |
| -------------------------------------------------- | -------------------------------------- |
| Below the expected output                          | Expected output minus delivered output |
| At or past the expected output, with an output cap | Output cap minus delivered output      |
| At or past the expected output, uncapped           | Unknown                                |

| Unknown remaining output on | Effect                                                        |
| --------------------------- | ------------------------------------------------------------- |
| Another request             | The request holds decode indefinitely                         |
| The checked request         | The window is the instant of its predicted prefill completion |

| Request             | Token interval                     |
| ------------------- | ---------------------------------- |
| Resident in decode  | Its decode engine's interval       |
| Every other request | Fleet mean of the engine intervals |

An engine's interval is its profiled token interval for a full batch at the current mean context, within the decode KV token bound, times the [decode correction](#71-load-definitions).

The capacity check applies when peak projected work over the window exceeds 1 slot.

The capacity check passes when that peak fits both budgets:

| Budget    | Per request                                | Fleet capacity                                                                                                     |
| --------- | ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------ |
| Slots     | 1                                          | Sum of `decode_max_requests`, each capped by `serving.decode_concurrency` when positive                            |
| KV tokens | Prompt plus delivered and remaining output | Sum of each engine's [decode KV token bound](../telemetry/02-Profiles.md#decode-capacity-derived-from-the-profile) |

The TPOT check passes in either case:

- a live decode engine meets `slo.tpot_s` with the request and its residents generating at the request's prefill completion
- the request misses `slo.tpot_s` on every idle decode engine

| Term       | Definition                                                                     |
| ---------- | ------------------------------------------------------------------------------ |
| Output cap | `max_completion_tokens`, or `max_tokens` when `max_completion_tokens` is unset |
| Bucket     | The request's power-of-two prompt and output-cap sizes                         |

| Request                                                    | Expected output                                                                          |
| ---------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| Capped, with three or more finished requests in its bucket | Output cap times the bucket's median delivered fraction                                  |
| Capped, with fewer finished requests in its bucket         | Output cap                                                                               |
| Uncapped                                                   | Median delivered output for its prompt bucket, or the fleet-wide median delivered output |

A shape overflow in the completion history keeps the bucket fractions and prompt-bucket medians last learned before the overflow until the overflow ages out.

The fleet-wide median delivered output is unknown while a shape overflow remains in the completion history.

### 4.2 Waiting, phase concurrency, and retries

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

With a positive `serving.decode_concurrency`, role control:

- prices each engine's decode capacity at that limit
- prices a limit below the profile's smallest measured batch at that batch's token interval
- floors decode load at decode residents on decode-role engines plus requests waiting for a decode slot, divided by decode engines times `serving.decode_concurrency`
- scores a smaller candidate decode pool with each departing engine's residents on that engine

Retained completion requests:

| Quantity                   | Value                                                                                                                           |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| Admission limit            | [`narwhal-serve --max-concurrent`](06-Fabric-and-Operations.md#18-cli-precedence) when set, otherwise `serving.max_connections` |
| Retained-request ceiling   | Admission limit plus `serving.queue_capacity`                                                                                   |
| New request at the ceiling | HTTP 429 before body parsing, counted as an [unsized offer](../http-api/06-SLO-and-Demand.md#unsized-offers)                    |

| Failure                                                                 | Result                                                |
| ----------------------------------------------------------------------- | ----------------------------------------------------- |
| Transient transport error before visible output                         | Retried when `serving.max_attempts` is greater than 1 |
| HTTP 408, 429, 500, 502, 503, or 504 before visible output              | Retried when `serving.max_attempts` is greater than 1 |
| Expired KV handoff                                                      | Retried when `serving.max_attempts` is greater than 1 |
| Permanent error, local data connection pool starvation, or cancellation | Ends the request                                      |
| Any failure after visible output                                        | Ends the request                                      |

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

| Failure                                                  | Client receives                                  |
| -------------------------------------------------------- | ------------------------------------------------ |
| Prefill failure                                          | HTTP error                                       |
| Non-streaming decode failure                             | HTTP error                                       |
| Streaming decode failure before the first output frame   | HTTP error                                       |
| Streaming decode failure after the first output frame    | Terminal stream error event                      |
| Request deadline expiry before the first output frame    | HTTP 504                                         |
| Request deadline expiry after the first output frame     | Terminal stream error event with `code: expired` |
| Request deadline expiry while client writes are blocked  | Immediate connection drop                        |

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

### 5.1 Prefix-cache pricing

For a request sized with exact token IDs, the router records each engine's cached leading prompt blocks from its [residency view](../http-api/05-Live-State.md#residency), up to the block before the final prompt token.

Each prefill placement rechecks those blocks against the engine's current view.

Projected-TTFT evaluations and role-split scoring reuse a waiting request's cache evidence checked within the last 0.25 s while every engine behind that evidence keeps a known residency view.

An engine that holds a cached prefix prices the request's prefill with its [warm prefill fit](../measure/01-Profile.md#warm-prefill-with-a-cached-prefix).

Decisions that use the warm price:

- which engines meet the TTFT budget, and their placement order
- predictive admission
- resident work
- pool load
- offered demand
- role-split scoring

Each recheck that changes a request's cache evidence reprices its arrival in offered demand.

Offered demand takes warm prices from engines that run prefill:

| Pricing            | Warm-price engines                      |
| ------------------ | --------------------------------------- |
| Demand estimate    | Live prefill-role engines               |
| Role-split scoring | Prefill engines of each candidate split |

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

Role pins, health holds, drains, ejections, and capacity limits apply to every placement.

The [request journal](../telemetry/01-Journal.md) records each placement priced with cache evidence in `cache_placement`.

## 6. Request deadlines and engine HTTP behaviour

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

| Field                       | Set from                                                             |
| --------------------------- | -------------------------------------------------------------------- |
| `serving.request_timeout_s` | Supported output length and client deadline                          |
| `serving.prefill_timeout_s` | Measurements of the longest admitted inputs under the supported load |

For diagnostic profiling, `narwhal-profile --observation-timeout-s` sets the probe HTTP timeout.

Prefill, tokenization, and health calls end when the first of their phase, connection, or pool timeouts expires.

### 6.2 First-token deadline and calibration

The `engine.first_token_timeout_s` window runs from before the decode HTTP stream opens to the first generated token.

Each inference probe leg, prefill and decode, has a budget of the larger of `engine.first_token_timeout_s` and `engine.health_timeout_s`.

Configure the first-token deadline:

1. Run [first-token deadline calibration](../deploy/06-Profile-and-Preflight.md#calibrate-the-first-token-deadline).
2. Set `engine.first_token_timeout_s` above the candidate it prints.
3. Set `engine.first_token_calibration_path` to its artifact.

The calibration artifact binds each engine to a generation digest:

| Engine evidence                             | Generation digest                                        |
| ------------------------------------------- | -------------------------------------------------------- |
| Attestation response with launch evidence   | Its [`launch_digest`](01-Fleet-Schema.md#33-attestation) |
| Other attestation response                  | Its `attestation_digest`                                 |
| Fleet config leaves `engine_contract` unset | Digest of the engine's process identity                  |

A change to an engine's generation digest makes the calibration artifact stale.

An engine relaunch during calibration makes the calibration artifact insufficient.

| Calibration artifact  | Preflight      | Router startup |
| --------------------- | -------------- | -------------- |
| Empty path            | Logs a warning | Logs a warning |
| Stale or insufficient | Fails          | Fails          |

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

| Input                                                                                    | Token count                                    |
| ---------------------------------------------------------------------------------------- | ---------------------------------------------- |
| Completion prompt that is a nonempty, flat list of nonnegative integer token IDs         | Local array length                             |
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

| Timeout                                                                                                              | Result             |
| -------------------------------------------------------------------------------------------------------------------- | ------------------ |
| Health or inference probe waits longer than `engine.pool_timeout_s` for a control connection                         | Inconclusive probe |
| Health probe exceeds `engine.health_timeout_s`                                                                       | Failed probe       |
| Health probe timeout surfaces more than 1.5 times `engine.health_timeout_s` after the probe's first connection event | Inconclusive probe |

| Caller                 | Inconclusive probe                      |
| ---------------------- | --------------------------------------- |
| Breaker verification   | Engine keeps its current health verdict |
| Liveness probe         | Engine keeps its current health verdict |
| Preflight reach        | Failed `/health` check                  |
| Readmission probe      | Engine stays ejected                    |
| Readmission validation | Failed `/health` check                  |

## 7. Role control

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

| Term                                                | Definition                                                                                                                                                     |
| --------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Decode correction                                   | The engine's ratio of live to profiled decode latency, bounded by `controller.reactive.decode_correction_min` and `controller.reactive.decode_correction_max`. |
| Corrected idle floor                                | The profile's [zero-contention decode interval](../telemetry/02-Profiles.md#profile-fields), multiplied by the decode correction.                              |
| Remaining time per output token (TPOT) budget       | The TPOT target minus the corrected idle floor.                                                                                                                |
| Observed token interval                             | The larger of the engine's mean token interval and its longest open inter-token gap.                                                                           |
| Decode load                                         | The observed token interval above the floor divided by the remaining budget, at least 0.                                                                       |
| Decode load at a floor that reaches the TPOT target | The raw interval-to-target ratio.                                                                                                                              |

A load of `1.0` means the phase has reached its target.

Controller decision details name load as pressure, such as the `mixed_pressure` eligibility rule and the `source_pressure_safe` flag.

### 7.2 Role floors

| Fleet               | Floor rule                                                                     |
| ------------------- | ------------------------------------------------------------------------------ |
| Two or more engines | `controller.min_prefill` plus `controller.min_decode` at most the engine count |
| One engine          | Both floors at `1`                                                             |

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
| --------------------------------------------------- | :-----: | ----------------------------------------------------------------------------------- | -------------------------------------------------------- |
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
| `controller.reactive.demand_rise_tolerance`         | `0.25`  | Maximum accepted short-horizon rise over long-horizon decode demand.                | Zero or greater                                          |
| `controller.reactive.decode_correction_min`         | `0.5`   | Lower bound on the live-to-profile decode correction.                               | Positive                                                 |
| `controller.reactive.decode_correction_max`         | `2.0`   | Upper bound on the live-to-profile decode correction.                               | At least `decode_correction_min`                         |
| `controller.reactive.decode_correction_alpha`       | `0.2`   | Fraction of each qualifying observation window applied to the correction.           | `(0, 1]`                                                 |
| `controller.reactive.decode_correction_min_samples` | `8`     | Required decode gaps before a window updates the correction.                        | At least 1                                               |
