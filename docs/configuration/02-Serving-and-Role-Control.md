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

In `predictive` mode, the router returns HTTP 429 when a request fails the time to first token (TTFT) check or the decode admission check. `open` mode disables predictive refusals.

[Admission and refusal semantics](../http-api/02-Admission-and-Responses.md#admission-and-refusal-semantics) lists the error and `Retry-After` value for each predictive refusal.

Before raising `serving.max_connections`, measure the in-flight load the fleet sustains while healthy.

#### Decode admission check

The decode admission check projects when each request holds a slot in the decode pool.

The check admits the request outright when the fleet has zero live decode engines, or when any live decode engine's profile or profiled `decode_max_requests` is unset.

Each request holds one slot. The pool's slot count is the sum of each live decode engine's `decode_max_requests`, capped per engine by `serving.decode_concurrency` when that setting is positive.

| Request                                    | Reaches decode                      | Holds a slot                                                                       |
| ------------------------------------------ | ----------------------------------- | ---------------------------------------------------------------------------------- |
| Request resident in decode                 |                                     | From now until its projected last token                                            |
| Request waiting for a decode slot          | Now                                 | From the earliest free slot until its projected last token                         |
| Request in prefill, or the checked request | At its predicted prefill completion | From the earliest free slot after it reaches decode until its projected last token |

The check assigns free slots to requests in the order they reach decode.

| Term           | Meaning                                                                               |
| -------------- | ------------------------------------------------------------------------------------- |
| Slot wait      | Time from the checked request's predicted prefill completion to the start of its slot |
| Projected TTFT | Time since arrival plus the prefill placement price, including queueing               |
| Request KV     | Key-value (KV) tokens for the prompt plus delivered and remaining output              |
| Output cap     | `max_completion_tokens`, or `max_tokens` when `max_completion_tokens` is unset        |
| Bucket         | The request's prompt length and output cap, each rounded up to a power of two         |

The check expects this much output from each request:

| Request                                                       | Expected output                                                                          |
| ------------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| Capped, with three or more finished requests in its bucket    | Output cap times the bucket's median delivered fraction                                  |
| Capped, with fewer than three finished requests in its bucket | Output cap                                                                               |
| Uncapped                                                      | Median delivered output for its prompt bucket, or the fleet-wide median delivered output |

Remaining output depends on how much output the request has delivered:

| Delivered output               | Remaining output                                                      |
| ------------------------------ | --------------------------------------------------------------------- |
| Below the expected output      | Expected output minus delivered output                                |
| At or past the expected output | Output cap minus delivered output, or unknown for an uncapped request |

When a request's remaining output is unknown:

- the checked request holds its slot for the instant the slot starts
- every other request holds its slot indefinitely

The check projects generation at these token intervals:

| Request                    | Token interval                     |
| -------------------------- | ---------------------------------- |
| Request resident in decode | Its decode engine's interval       |
| Every other request        | Fleet mean of the engine intervals |

An engine's interval is its profiled token interval times the [decode correction](#71-load-definitions). The profiled interval assumes a full batch at the current mean context, within the decode KV token bound.

The check admits the request when all three of these pass:

| Check     | Passes when                                                                                                                                                                                                                                  |
| --------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Slot wait | The slot wait is zero, or the projected TTFT plus the slot wait fits the [TTFT budget](../http-api/02-Admission-and-Responses.md#admission-and-refusal-semantics)                                                                            |
| KV tokens | Peak request KV during the checked request's hold fits the sum of the live decode engines' [decode KV token bounds](../telemetry/02-Profiles.md#decode-capacity-derived-from-the-profile), or the request is alone in decode during its hold |
| TPOT      | At the start of the request's slot, a live decode engine meets `slo.tpot_s` with the request and the residents still generating on that engine, or the request misses `slo.tpot_s` on every idle decode engine                               |

#### Retry pricing

In `predictive` mode, only an original request's first attempt is priced against the TTFT check. An attempt after the first skips the TTFT check and runs the decode admission check, where the slot-wait check uses the retry's predicted prefill completion in place of its projected TTFT. A retry that fails the decode admission check receives HTTP 429 with that check's cause. Its journal attempt entry carries `retry_reason` `not_dispatched`.

### 4.2 Waiting, phase concurrency, and retries

| Field                         | Default    | Meaning                                                                                                               | Values                                                                     |
| ----------------------------- | :--------: | --------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| `serving.queue_capacity`      | `0`        | Maximum requests waiting for admission.                                                                               | `0` rejects immediately at saturation                                      |
| `serving.queue_timeout_s`     | `0.0`      | Maximum admission wait, capped by the original request deadline.                                                      | Positive when `serving.queue_capacity` is positive                         |
| `serving.prefill_concurrency` | `0`        | Maximum resident prefill requests per engine.                                                                         | Positive when `serving.queue_capacity` is positive                         |
| `serving.decode_concurrency`  | `0`        | Maximum resident decode requests per engine.                                                                          | Positive when `serving.queue_capacity` is positive                         |
| `serving.handoff_timeout_s`   | `0.0`      | Maximum KV handoff age from the start of the prefill HTTP request, with `0` meaning the request deadline.             | Positive and below the verified backend KV lease when queueing            |
| `serving.max_attempts`        | `1`        | Maximum complete prefill and decode attempts per original request.                                                    | 1 to 3                                                                     |
| `serving.retry_base_s`        | `0.1`      | Initial exponential-backoff ceiling.                                                                                  | Positive                                                                   |
| `serving.retry_cap_s`         | `1.0`      | Maximum backoff ceiling.                                                                                              | At least `serving.retry_base_s`                                            |
| `serving.retry_budget`        | `10`       | Starting and maximum size of the retry-credit pool, with each retry spending one credit when it dispatches.          | Zero or greater                                                            |
| `serving.retry_replenish`     | `0.1`      | Credits added after each successful original request.                                                                 | 0 to 1                                                                     |
| `serving.max_request_bytes`   | `4194304`  | Maximum HTTP request-body size.                                                                                       | At least 1                                                                 |
| `serving.max_response_bytes`  | `16777216` | Maximum bytes retained for each non-streaming attempt, and for the metadata a stream buffers before its first output. | At least 1                                                                 |

When `serving.decode_concurrency` is positive, role control prices each engine's decode capacity at that limit. Each candidate split's decode load is at least:

```text
(decode residents on decode-role engines + requests waiting for a decode slot)
  / (serving.decode_concurrency * max(current decode engines, candidate decode engines))
```

The admission limit is [`narwhal-serve --max-concurrent`](06-Fabric-and-Operations.md#18-cli-precedence) when set, otherwise `serving.max_connections`.

The router retains at most the admission limit plus `serving.queue_capacity` completion requests. At that ceiling, the router answers a new request with HTTP 429 before parsing its body and counts it as an [unsized offer](../http-api/06-SLO-and-Demand.md#unsized-offers).

When `serving.max_attempts` is greater than 1, the router retries an attempt that fails before visible output with any of these:

- a transient transport error
- an HTTP 408, 429, 500, 502, 503 or 504 response
- an expired KV handoff

Any of these ends the request:

- a permanent error
- starvation of the local data connection pool
- cancellation
- any failure after visible output

A retry avoids each engine whose leg failed earlier in the same request. When no engine remains for a role, the request ends with HTTP 503. [Retries](../http-api/03-Backend-and-Failures.md#retries) gives the placement rules, the retry credit and the exact-count failures that move between engines.

The original request deadline covers tokenization, queue wait, retry backoff, engine work, and client writes.

Tune queueing and retries:

1. Set queue capacity, phase concurrency, and deadlines from the workload's measured latency and capacity.
2. Verify that the backend releases abandoned KV handoffs when the lease expires.
3. After any change to queueing, concurrency limits, KV handoff expiry, retries, or byte limits, measure again.

### 4.3 Streaming failure semantics

A streaming response holds its HTTP status and headers until the first output frame.

[Engine failure handling](../http-api/03-Backend-and-Failures.md#engine-failure-handling) gives the client response for each prefill and decode failure.

Request deadline expiry ends a streaming response this way:

| Deadline expires                | Result                                                                 |
| ------------------------------- | ---------------------------------------------------------------------- |
| Before the first output frame   | The client receives HTTP 504                                           |
| After the first output frame    | The client receives a terminal stream error event with `code: expired` |
| While client writes are blocked | The router drops the connection immediately                            |

Treat an error event, or a stream that ends before the success terminator, as a failed response.

Fit client-side retries inside the caller's remaining deadline.

---

## 5. Placement

Placement selects the lowest-cost engine that passes the role, availability, exclusion, and projected service-level objective (SLO) filters. Ties go to the lowest instance ID.

When every candidate violates its projected SLO, placement selects the lowest-cost candidate as an unserved placement.

A live role change:

- applies to new placements immediately
- leaves resident requests on their current engine and reservation until completion or cancellation
- keeps lifecycle drains, quarantine, ejections, and restart holds in place

### 5.1 Prefix-cache pricing

For a request sized from exact token IDs, the router records the leading prompt blocks each engine has cached, from that engine's [residency view](../http-api/05-Live-State.md#residency).

Each prefill placement rechecks those blocks against the engine's current view.

When an engine holds a cached prefix, the router prices prefill on that engine with its [warm prefill fit](../measure/01-Profile.md#warm-prefill-with-a-cached-prefix).

These decisions use the warm price:

- which engines meet the TTFT budget, and their placement order
- predictive admission
- resident work
- pool load
- offered demand
- role-split scoring

Offered demand takes warm prices from the engines that run prefill:

| Offered demand in  | Warm prices from                       |
| ------------------ | -------------------------------------- |
| Demand estimate    | Live prefill-role engines              |
| Role-split scoring | Each candidate split's prefill engines |

The router prices a request on the cold curve of its full input when any of these holds:

- the router used its local length estimate
- the request carries multimodal content
- the request sets `truncate_prompt_tokens`, `documents`, or `reasoning_effort`
- the fleet config leaves `engine_contract` unset
- the fleet's `engine_contract` enables speculative decoding
- the engine's residency is unknown
- the engine's current view drops the prefix before placement
- the engine's profile holds only a cold fit
- the case lies outside the warm fit's measured domain

The request journal records each placement priced with cache evidence in [`cache_placement`](../telemetry/01-Journal.md#cache-placement).

---

## 6. Request deadlines and engine HTTP behaviour

| Field                                 | Default | Meaning                                                                                     | Values                                        |
| ------------------------------------- | ------- | ------------------------------------------------------------------------------------------- | --------------------------------------------- |
| `serving.request_timeout_s`           | `600.0` | End-to-end completion deadline from HTTP ingress through response delivery.                 | Positive                                      |
| `serving.prefill_timeout_s`           | `120.0` | Elapsed-time deadline for the prefill leg.                                                  | Positive, at most `serving.request_timeout_s` |
| `engine.first_token_timeout_s`        | `2.5`   | Deadline to the first decode token.                                                         | Positive, at most `serving.request_timeout_s` |
| `engine.first_token_calibration_path` | `""`    | Path to a completed first-token calibration artifact under `runs/`.                         |                                               |
| `engine.decode_read_timeout_s`        | `60.0`  | Maximum silent interval between decode chunks after the first token.                        | `0` disables the gap limit                    |
| `engine.tokenize`                     | `true`  | Requests exact token counts for text and chat input from the dialect tokenization endpoint. |                                               |
| `engine.tokenize_timeout_s`           | `2.0`   | Elapsed-time deadline for an exact token count.                                             | Positive                                      |
| `engine.chars_per_token`              | `3.8`   | Characters per token for the fallback estimate.                                             | Positive                                      |
| `engine.connect_timeout_s`            | `10.0`  | TCP connect deadline for engine requests.                                                   | Positive                                      |
| `engine.pool_timeout_s`               | `5.0`   | Maximum wait for a connection from the data or control connection pool.                     | Positive                                      |
| `engine.health_timeout_s`             | `5.0`   | HTTP I/O timeout for preflight, breaker, readmission, and residency requests.               | Positive                                      |

### 6.1 Request and prefill deadlines

Set the deadlines from these inputs:

| Field                       | Set from                                                             |
| --------------------------- | -------------------------------------------------------------------- |
| `serving.request_timeout_s` | Supported output length and the client deadline                      |
| `serving.prefill_timeout_s` | Measurements of the longest admitted inputs under the supported load |

For diagnostic profiling, `narwhal-profile --observation-timeout-s` sets the probe HTTP timeout.

A prefill, tokenization, or health call ends at the first of its phase, connection, or pool timeouts to expire.

### 6.2 First-token deadline and calibration

The `engine.first_token_timeout_s` window runs from before the decode HTTP stream opens to the first generated token.

The budget for each inference probe leg, prefill and decode, is the larger of `engine.first_token_timeout_s` and `engine.health_timeout_s`.

Configure the first-token deadline:

1. Run [first-token deadline calibration](../deploy/06-Profile-and-Preflight.md#calibrating-the-first-token-deadline).
2. Set `engine.first_token_timeout_s` above the candidate it prints.
3. Set `engine.first_token_calibration_path` to its artifact.

The calibration artifact binds each engine to the same process generation as its [saved profiles](../telemetry/02-Profiles.md#validating-the-engine-cost-model).

Preflight and router startup respond to these conditions:

| Condition                                             | Artifact     | Preflight and router startup |
| ----------------------------------------------------- | ------------ | ---------------------------- |
| `engine.first_token_calibration_path` is empty        |              | Log a warning                |
| An engine relaunched with the same process generation |              | Label the engine `reused`    |
| An engine's process generation changed                | Stale        | Fail                         |
| An engine relaunched during calibration               | Insufficient | Fail                         |

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

The router counts a request's input tokens this way:

| Input                                                                                    | Token count                                                     |
| ---------------------------------------------------------------------------------------- | --------------------------------------------------------------- |
| Completion prompt that is a nonempty, flat list of nonnegative integer token IDs         | Local array length                                              |
| Text or chat input, with `engine.tokenize` set to `true` and a dialect exact-count route | Exact count from that route, within `engine.tokenize_timeout_s` |
| Other text or chat input                                                                 | Estimate from `engine.chars_per_token`                          |

When the exact-count call fails, the client receives the engine error before placement.

Measure `engine.chars_per_token` for the served tokenizer and for every dialect that uses the estimate.

### 6.5 Connection, pool, and health timeouts

Set these timeouts from latency measured under the intended load:

| Field                      | Measured latency             |
| -------------------------- | ---------------------------- |
| `engine.connect_timeout_s` | Connection setup             |
| `engine.pool_timeout_s`    | Pool waits                   |
| `engine.health_timeout_s`  | Health and identity requests |

These timeouts decide a probe's outcome:

| Probe                     | Condition                                                                              | Outcome      |
| ------------------------- | -------------------------------------------------------------------------------------- | ------------ |
| Health or inference probe | Waits longer than `engine.pool_timeout_s` for a control connection                     | Inconclusive |
| Health probe              | Times out within 1.5 times `engine.health_timeout_s` of getting its control connection | Failed       |
| Health probe              | Times out later than that                                                              | Inconclusive |

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

| Field                                       | Default | Meaning                                                                                              | Values                               |
| ------------------------------------------- | ------- | ---------------------------------------------------------------------------------------------------- | ------------------------------------ |
| `controller.advisory`                       | `false` | Holds current roles and records proposed role splits with reasons.                                   |                                      |
| `controller.monitor_interval_s`             | `1.0`   | Delay between engine monitoring passes and between residency refreshes.                              | Positive                             |
| `controller.monitor_failure_limit`          | `5`     | Consecutive engine monitoring passes with a stage failure before degraded state stops new admission. | At least 1                           |
| `controller.min_prefill`                    | `1`     | Minimum live prefill engines that role-controller moves preserve.                                    | At least 1                           |
| `controller.min_decode`                     | `1`     | Minimum live decode engines that role-controller moves preserve.                                     | At least 1                           |
| `controller.thresholds.expand`              | `1.0`   | SLO-relative pool load that starts reactive expansion.                                               | Positive                             |
| `controller.thresholds.shrink`              | `0.5`   | Maximum projected source load for ordinary consolidation.                                            | Zero or greater, lower than `expand` |
| `controller.thresholds.cooldown_s`          | `10.0`  | Minimum time between prefill-to-decode moves.                                                        | Zero or greater                      |
| `controller.thresholds.sustained_intervals` | `3`     | Confirmations required for moves that need them.                                                     | At least 1                           |
| `controller.thresholds.dwell_s`             | `0.0`   | Minimum time an engine stays in a new role.                                                          | Zero or greater                      |
| `controller.thresholds.panic_ratio`         | `0.0`   | Multiple of `expand` for the prefill-to-decode cooldown bypass.                                      | `0` for off, or at least 1           |
| `controller.thresholds.flip_resident_guard` | `0`     | Maximum resident decode streams allowed on a decode-to-prefill donor.                                | `0` disables the guard               |
| `controller.flip_history`                   | `1000`  | Maximum role-change records the router retains and exposes in `/narwhal/state`.                      | At least 1                           |

### 7.1 Load definitions

Prefill load is:

```text
predicted prefill work / TTFT target
```

Decode load is:

```text
max(0, observed token interval - corrected idle floor) / remaining TPOT budget
```

| Term                                          | Meaning                                                                                                                                                       |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Observed token interval                       | Larger of the engine's mean token interval and its longest open inter-token gap                                                                               |
| Corrected idle floor                          | The profile's [zero-contention decode interval](../telemetry/02-Profiles.md#profile-fields) times the decode correction                                       |
| Remaining time per output token (TPOT) budget | TPOT target minus the corrected idle floor                                                                                                                    |
| Decode correction                             | The engine's ratio of live to profiled decode latency, bounded by `controller.reactive.decode_correction_min` and `controller.reactive.decode_correction_max` |

When the corrected idle floor reaches the TPOT target, decode load is the raw interval-to-target ratio.

A load of `1.0` means the phase has reached its target.

Controller decision details call load "pressure", as in the `mixed_pressure` eligibility rule and the `source_pressure_safe` flag.

### 7.2 Role floors

In a fleet of two or more engines, `controller.min_prefill` plus `controller.min_decode` is at most the engine count. A one-engine fleet has both floors at `1`.

When pins, drains, quarantine, or health ejections leave too few movable engines for a floor, Narwhal reports a floor breach.

Every role change preserves `controller.min_decode`.

When live decode engines drop below `controller.min_decode`, engine monitoring moves one eligible engine to decode per pass.

Floor recoveries skip some move timers:

| Recovery      | Cooldown | Per-engine dwell |
| ------------- | -------- | ---------------- |
| Decode floor  | Skipped  | Applied          |
| Prefill floor | Skipped  | Skipped          |

Both floor recoveries:

- honor pins, availability, floor limits, and advisory mode
- add each applied move to the role-change records that `controller.flip_history` caps

### 7.3 Resident work during role changes

A decode-to-prefill move requires:

- the new split and resident decode batches inside the profile's decode domain
- KV capacity for the resident work

### 7.4 Advisory rollout

Before allowing role changes in production, run advisory mode against recorded traffic.

Enable advisory mode:

```json
{
  "controller": {
    "advisory": true
  }
}
```

`/narwhal/state` and Prometheus expose each proposal's prefill and decode counts, caller, reason, and result.

### 7.5 Adjacent-split decisions

An adjacent split differs from the current split by one engine moved between prefill and decode.

The worst projected SLO ratio of a split is the largest of its projected TTFT, TPOT, and decode queueing ratios.

| Move              | Projected source load                                            |
| ----------------- | ---------------------------------------------------------------- |
| Prefill-to-decode | The candidate's projected TTFT ratio                             |
| Decode-to-prefill | The larger of the candidate's TPOT and decode queueing ratios    |

The decode queueing ratio projects the slots of the [decode admission check](#decode-admission-check) onto the split's decode engines:

```text
max over waiting decode requests and requests in prefill:
  slot wait / (slo.ttft_s - time since arrival - time until the request reaches decode)
```

The split's slot count is its decode engine count times the live mean slot count per decode engine.

A request at or past its TTFT deadline when it reaches decode contributes zero.

Adjacent-split decisions use these spans:

| Span              | Length                                                                                                                   | Default |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------ | :-----: |
| Window            | `controller.reactive.window_s`                                                                                           |  120 s  |
| Settled run       | `controller.reactive.evidence_span_s`                                                                                    |   60 s  |
| Confirmation span | `controller.reactive.step_s` times max(`controller.reactive.confirmations`, `controller.thresholds.sustained_intervals`) |   15 s  |
| Reversal lookback | `controller.reactive.window_s` plus `controller.reactive.evidence_span_s`                                                |  180 s  |

Each scored decision records in `eligibility_rule` the first of these rules that matches:

1. `projected_ttft_recovery`
2. `mixed_pressure`
3. `settled_departure`
4. `steady_demand`
5. `source_shrink`

Ordinary consolidation, the `source_shrink` rule, requires both:

- projected source load at or below `controller.thresholds.shrink`
- reduction in the worst projected SLO ratio of at least `controller.reactive.movement_margin`

The `mixed_pressure` rule moves one decode engine to prefill when all of these hold:

- projected source load is above `controller.thresholds.shrink`
- the [prefill recovery ratio](../http-api/06-SLO-and-Demand.md#prefill-recovery-ratio) is at or above `controller.thresholds.expand`
- every engine has a profile
- the move improves the worst projected SLO ratio by more than zero and by at least `controller.reactive.movement_margin`

The settled run is the time the current split has held without a better adjacent split. It restarts when any of these happens:

- the role controller attempts a move
- an evaluation stops before scoring splits
- for `controller.reactive.step_s`, an adjacent split improves the worst projected SLO ratio by at least `controller.reactive.movement_margin` on window or confirmation-span demand

A departure reverses when all of these hold as it opens:

- the role controller applied at least one [adjacent-split move](../http-api/05-Live-State.md#role-change-history) within the preceding reversal lookback
- each of those moves went opposite the departure's direction
- the latest of those moves is at least `controller.reactive.evidence_span_s` old

Departures need these settled runs:

| Departure | Settled-run length                                                | Default |
| --------- | ----------------------------------------------------------------- | :-----: |
| Reversing | `controller.reactive.evidence_span_s` minus the confirmation span |   45 s  |
| Other     | `controller.reactive.evidence_span_s`                             |   60 s  |

The role controller opens a departure when all of these hold:

- the current settled run is at least the departure's settled-run length
- confirmation-span demand for either phase differs from window demand by more than `controller.reactive.demand_rise_tolerance` times the larger estimate
- on confirmation-span demand, an adjacent split improves the worst projected SLO ratio by at least `controller.reactive.movement_margin`

The `settled_departure` rule moves one engine toward that split when both hold on confirmation-span demand:

- projected source load at or below `controller.thresholds.shrink`
- reduction in the worst projected SLO ratio of at least `controller.reactive.movement_margin`

After its first move, a departure leads until either of these has held for `controller.reactive.step_s`:

- on confirmation-span demand, the best adjacent split lies opposite the departure's direction and improves the worst projected SLO ratio by at least `controller.reactive.movement_margin`
- confirmation-span demand for each phase is within `controller.reactive.demand_rise_tolerance` times the larger estimate of window demand

The departure's later moves use this demand and rule:

| Departure | Later move                                                                               | Demand                   | Rule                |
| --------- | ---------------------------------------------------------------------------------------- | ------------------------ | ------------------- |
| Reversing | From the first move until the lead ends                                                  | Confirmation-span demand | `source_shrink`     |
| Other     | From `controller.reactive.evidence_span_s` after the departure opens until the lead ends | Confirmation-span demand | `source_shrink`     |
| Any       | Every other later move                                                                   | Window demand            | First matching rule |

When `mixed_pressure` applies to the departure's split on window demand, that split keeps window demand and the `mixed_pressure` rule.

A departure closes at the first of these:

- `controller.reactive.window_s` passes after the departure opens
- before the first move, the best adjacent split on confirmation-span demand lies in the other direction
- before the first move, the best adjacent split on confirmation-span demand improves the worst projected SLO ratio by less than `controller.reactive.movement_margin`
- after the first move, the role controller applies a window-demand move in the departure's direction

Once a departure has moved, it holds moves in the opposite direction until it closes.

Demand is steady when both hold:

- for the last `controller.reactive.evidence_span_s`, confirmation-span demand and window demand for each phase differ by at most `controller.reactive.demand_rise_tolerance` times the larger estimate
- the arrival-evidence window is closed

The `steady_demand` rule moves one engine under steady demand when both hold:

- projected source load above `controller.thresholds.shrink` and at or below `controller.thresholds.expand`
- reduction in the worst projected SLO ratio of at least `controller.reactive.movement_margin`

Projected-TTFT recovery evaluations leave the settled run, `steady_demand_s`, and an open departure unchanged.

A standby router or a lifecycle hold pauses role control and restarts the settled run and `steady_demand_s`.

One confirmation is enough in three cases:

- the move comes from a projected-TTFT recovery evaluation
- the eligibility rule is `settled_departure`
- all of these hold:
  - [demand](../http-api/06-SLO-and-Demand.md#demand-completeness) is complete
  - the eligibility rule is `source_shrink`
  - the destination phase's current SLO ratio meets or exceeds `controller.thresholds.expand`

Every other move needs this many confirmations:

```text
max(
  controller.reactive.confirmations,
  controller.thresholds.sustained_intervals
)
```

Any of these resets the confirmation sequence:

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

Decode-to-prefill consolidation waits for the arrival-evidence window to close. The window closes when either of these holds:

- `controller.reactive.evidence_span_s` has elapsed with at least `controller.reactive.evidence_min_arrivals` samples
- `controller.reactive.evidence_max_span_s` has elapsed under sparse traffic

With enough samples in the short horizon of `controller.reactive.evidence_span_s`, consolidation pauses when:

```text
short_horizon_demand > long_horizon_demand * (1 + controller.reactive.demand_rise_tolerance)
```

A first-token timeout or a prefill-to-decode recovery move resets the arrival-evidence window.

The `source_shrink` and `settled_departure` rules move engines toward decode while the window is open.

The `steady_demand` rule requires a closed arrival-evidence window for moves in either direction.

Live state and metrics expose the short and long demand estimates, the arrival-evidence window state, and the gate blocking movement.

### 7.7 Reactive-controller parameters

| Field                                               | Default | Meaning                                                                                                                                                                                                                                                                                                                 | Values                                                   |
| --------------------------------------------------- | :-----: | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------- |
| `controller.reactive.window_s`                      | `120.0` | Demand-estimation window, the maximum time a departure stays open, and part of the reversal lookback.                                                                                                                                                                                                                   | Positive                                                 |
| `controller.reactive.confirmations`                 |   `2`   | Required consecutive identical adjacent proposals for moves that need confirmation, and a factor of the confirmation span.                                                                                                                                                                                              | At least 1                                               |
| `controller.reactive.utilization`                   |  `0.8`  | Fraction of each engine treated as usable capacity.                                                                                                                                                                                                                                                                     | Greater than 0, at most 1                                |
| `controller.reactive.min_arrivals`                  |  `10`   | Minimum arrival count required for a decision.                                                                                                                                                                                                                                                                          | At least 1                                               |
| `controller.reactive.demand_floor`                  |  `0.5`  | Minimum accepted demand signal.                                                                                                                                                                                                                                                                                         | Positive                                                 |
| `controller.reactive.movement_margin`               | `0.05`  | Required reduction in worst projected SLO ratio before movement.                                                                                                                                                                                                                                                        | `[0, 1)`                                                 |
| `controller.reactive.step_s`                        |  `5.0`  | Minimum interval between scheduled adjacent-split evaluations and the unit of the confirmation span.                                                                                                                                                                                                                    | Positive                                                 |
| `controller.reactive.evidence_span_s`               | `60.0`  | Minimum recent-arrival span for decode-to-prefill consolidation, steady-demand duration, settled-run length, part of the reversal lookback, the minimum age of the latest move a reversing departure reverses, and the departure age from which other leading departures price later moves on confirmation-span demand. | Positive, at most `evidence_max_span_s`                  |
| `controller.reactive.evidence_max_span_s`           | `120.0` | Maximum evidence duration under sparse traffic.                                                                                                                                                                                                                                                                         | Positive, at least `evidence_span_s`, at most `window_s` |
| `controller.reactive.evidence_min_arrivals`         |  `10`   | Minimum samples within the evidence span before decode-to-prefill consolidation.                                                                                                                                                                                                                                        | At least 1                                               |
| `controller.reactive.demand_rise_tolerance`         | `0.25`  | Maximum accepted short-horizon rise over long-horizon decode demand, and the fraction of the larger estimate within which two demand spans match.                                                                                                                                                                       | Zero or greater                                          |
| `controller.reactive.decode_correction_min`         |  `0.5`  | Lower bound on the live-to-profile decode correction.                                                                                                                                                                                                                                                                   | Positive                                                 |
| `controller.reactive.decode_correction_max`         |  `2.0`  | Upper bound on the live-to-profile decode correction.                                                                                                                                                                                                                                                                   | At least `decode_correction_min`                         |
| `controller.reactive.decode_correction_alpha`       |  `0.2`  | Fraction of each qualifying observation window applied to the correction.                                                                                                                                                                                                                                               | `(0, 1]`                                                 |
| `controller.reactive.decode_correction_min_samples` |   `8`   | Required decode gaps before a window updates the correction.                                                                                                                                                                                                                                                            | At least 1                                               |
