# Serving and role control

## 4. Request admission and bounded serving

By default, Narwhal dispatches each admitted request straight away, with one prefill attempt and one decode attempt. The bounded-serving settings below add waiting for admission, per-phase concurrency limits, and retries.

### 4.1 Global admission

| Field                        | Default        | Meaning                                                                                                                                                                            |
| ---------------------------- | -------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `serving.admission`          | `"predictive"` | `predictive` prices every prefill path against the TTFT target and keeps aggregate placement inside the single-phase region covered by measurements. `open` turns off both checks. |
| `serving.admission_margin`   | `0.0`          | Fraction added to the TTFT admission budget to reduce churn at the boundary. Nonnegative.                                                                                          |
| `serving.max_connections`    | `512`          | Global limit on admitted requests, and the size of the HTTP data pool. At least 1.                                                                                                 |
| `engine.control_connections` | `0`            | HTTP connections reserved for health and recovery. `0` means two per engine, with a minimum of four. Nonnegative.                                                                  |

Under predictive admission, a request gets HTTP 429 when even its cheapest prefill path would exceed the TTFT budget. If backlog caused the refusal, the response carries `Retry-After` with the projected wait. If the prompt would miss the target with no backlog at all, the error envelope tells the caller to shorten the prompt or raise the TTFT target.

`narwhal-serve` rejects a `--max-concurrent` override above `serving.max_connections`, which keeps admission inside the dispatch pool. Before raising that limit, measure how much in-flight load the fleet can sustain while staying healthy.

### 4.2 Waiting, phase concurrency, and retries

| Field                         | Default    | Meaning                                                                                                                                                                                                       |
| ----------------------------- | ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `serving.queue_capacity`      | `0`        | Maximum requests waiting for admission. `0` rejects immediately at saturation.                                                                                                                                |
| `serving.queue_timeout_s`     | `0.0`      | Maximum admission wait, capped by the original request deadline. Must be positive when queueing is enabled.                                                                                                   |
| `serving.prefill_concurrency` | `0`        | Resident prefill requests per engine. Queueing requires a positive measured value.                                                                                                                            |
| `serving.decode_concurrency`  | `0`        | Resident decode requests per engine. Queueing requires a positive measured value.                                                                                                                             |
| `serving.handoff_timeout_s`   | `0.0`      | Caps KV-handoff age, measured from the start of the prefill HTTP request. Queueing or retry requires a positive cap below the verified backend lease. At `0`, the original request deadline supplies the cap. |
| `serving.max_attempts`        | `1`        | Maximum complete prefill/decode attempts per original request, from 1 to 3.                                                                                                                                   |
| `serving.retry_base_s`        | `0.1`      | Initial exponential-backoff ceiling. Full jitter samples between zero and this ceiling.                                                                                                                       |
| `serving.retry_cap_s`         | `1.0`      | Maximum backoff ceiling. At least the base value.                                                                                                                                                             |
| `serving.retry_budget`        | `10`       | Initial and maximum size of the retry-credit pool. Each retry spends one credit.                                                                                                                              |
| `serving.retry_replenish`     | `0.1`      | Credits added after each successful original request, from zero to one.                                                                                                                                       |
| `serving.max_request_bytes`   | `4194304`  | Maximum HTTP request-body size, enforced while the body is read.                                                                                                                                              |
| `serving.max_response_bytes`  | `16777216` | Maximum retained bytes for each non-streaming attempt, and for the streaming metadata buffer before output starts.                                                                                            |

While it is reading request bodies, waiting for admission, or writing responses, Narwhal holds at most `serving.max_connections + serving.queue_capacity` completion requests. A request that arrives at that ceiling gets HTTP 429 before its body is parsed and is recorded as an unsized offer. Time spent queued or waiting for phase dispatch still counts as demand, and a retry keeps the original arrival time, deadline, and reservation.

Retries happen only before any output has reached the client, and only for transient transport failures and HTTP 408, 429, 500, 502, 503, and 504. Each retry starts with fresh prefill ownership, as does recovery from an expired handoff. For non-streaming output, the partial body from the failed attempt is discarded before the retry. A permanent error, starvation of the local HTTP pool, cancellation, or any failure after visible output ends the request.

The original deadline covers the whole request: tokenization, queue wait, retry backoff, engine work, and writes to the client.

Set `serving.handoff_timeout_s` below the producer's KV lease, and check separately that the backend releases abandoned handoffs when the lease expires. Queue capacity, phase concurrency, and deadlines should come from measured workload latency and capacity, while the byte limits simply cap how much data the router holds. Every one of these settings changes deployment capacity, so repeat the workload measurement after changing any of them.

### 4.3 Streaming failure semantics

Prefill failures and non-streaming decode failures come back as HTTP errors. Streaming works differently, because the response commits to HTTP 200 before decode starts. A decode failure after that point, even one before the first generated token, arrives as a terminal error event in the stream. When the request deadline runs out, Narwhal sends `code: expired` and closes the stream. Client backpressure closes the connection immediately.

Clients should treat an error event, or a stream that ends without the success terminator, as a failed response. Any client-side retry has to fit within what is left of the caller's deadline.

---

## 5. Placement

Placement narrows the candidate engines by role, then availability, then exclusions, then projected SLO compliance, and picks the cheapest engine that remains. Ties between equal-cost candidates are broken deterministically by instance ID. If every candidate would miss its projected SLO, Narwhal records the placement as unserved and falls back to the cheapest candidate. Engine-side prefix caching plays no part in these decisions.

A live role change applies to new placements straight away. Requests already resident keep their engine and reservation until they complete or are canceled, and lifecycle drains, quarantine, ejection, and restart holds all stay in force while roles change.

Being admitted does not guarantee completion. If the fleet admits more work than its engines can drain before KV handoffs expire, decode can fail for requests that admission accepted.

---

## 6. Request deadlines and engine HTTP behavior

| Field                                 | Default                | Meaning                                                                                                                                                              |
| ------------------------------------- | ---------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `profiles.path`                       | `"runs/profiles.json"` | Profile store read by the router and written by `narwhal-profile`.                                                                                                   |
| `serving.request_timeout_s`           | `600.0`                | End-to-end completion deadline, from HTTP ingress through response delivery. Positive.                                                                               |
| `serving.prefill_timeout_s`           | `120.0`                | Deadline for the prefill leg. Positive and at most `serving.request_timeout_s`.                                                                                      |
| `recovery.failure_quarantine_s`       | `0.0`                  | How long a failed engine stays out of placement while its health state settles. `0` disables quarantine.                                                             |
| `engine.decode_read_timeout_s`        | `60.0`                 | Longest silent interval allowed between decode chunks. `0` disables the gap limit.                                                                                   |
| `engine.first_token_timeout_s`        | `2.5`                  | Deadline for the first decode token, and for each functional-verification leg. Positive and at most `serving.request_timeout_s`.                                     |
| `engine.first_token_calibration_path` | `""`                   | Path to a completed first-token calibration artifact under `runs/`. Empty means no calibration is configured.                                                        |
| `engine.tokenize`                     | `true`                 | Asks the dialect's tokenization endpoint for the exact length of text and chat inputs. Token-ID prompts are counted locally.                                         |
| `engine.tokenize_timeout_s`           | `2.0`                  | Deadline for the exact token count. Positive. Errors from an available tokenizer route fail the request before placement.                                            |
| `engine.chars_per_token`              | `3.8`                  | Characters-per-token ratio used as a fallback. Positive.                                                                                                             |
| `engine.pool_timeout_s`               | `5.0`                  | Longest wait for an engine HTTP connection on the serving or control pools. Running out of probe connections leaves the engine's health verdict unchanged. Positive. |
| `engine.connect_timeout_s`            | `10.0`                 | TCP-connect deadline for engine requests. Positive.                                                                                                                  |
| `engine.health_timeout_s`             | `5.0`                  | HTTP I/O timeout for preflight, breaker, and readmission health probes. Positive.                                                                                    |

Set `engine.first_token_timeout_s` above the candidate value from a [completed crossed-handoff calibration](../deploy/06-Profile-and-Preflight.md#calibrate-the-first-token-deadline), and point `engine.first_token_calibration_path` at that artifact. `narwhal-check` compares the artifact with the configured value and the current engine generations. A stale or insufficient artifact fails preflight and blocks router startup. An empty path is allowed, but preflight warns about it and so does the router's startup log.

The first-token clock starts before the decode HTTP stream is opened and stops when the first generated token arrives, so connection setup and response-header delays come out of the same budget. Breaker verification applies the limit separately to each complete prefill and decode verification leg.

After the first token, `engine.decode_read_timeout_s` limits the silent gap between transport chunks. Partial SSE lines and metadata chunks both reset the timer. Choose the value from measured inter-chunk gaps and the service's failure budget, or set it to `0` so that the overall request deadline bounds the stream after the first token:

```json
{
  "engine": {
    "decode_read_timeout_s": 0
  }
}
```

`serving.request_timeout_s` covers the entire request, including decode streaming, so derive it from the supported output length and the client's deadline. The prefill and first-token limits cannot exceed it. Set `serving.prefill_timeout_s` from measurements of the longest admitted inputs under the supported load. For diagnostic profiling, `narwhal-profile --observation-timeout-s` sets the probe's HTTP timeout instead.

When prefill, tokenization, and health calls apply their own phase timeout, the connection and pool limits still apply, and whichever budget expires first ends the call. Base `engine.connect_timeout_s` and `engine.pool_timeout_s` on connection setup times and pool waits measured under the intended load, and base `engine.health_timeout_s` on the health and identity latency you see under that same load. A health probe that times out counts as a failed liveness observation.

For text and chat inputs with `engine.tokenize` enabled, Narwhal asks the dialect's exact-count route for the input length. That route must answer within `engine.tokenize_timeout_s`, and if the call fails the client gets an engine error. When counting is disabled, or the dialect has no exact-count route, Narwhal estimates the length with `engine.chars_per_token`. The same ratio feeds the quadratic prefill estimate, so measure it for the served tokenizer and for every dialect that relies on the fallback. A completion prompt given as a nonempty, flat list of nonnegative integer token IDs is always counted locally by its length, whatever `engine.tokenize` is set to.

---

## 7. Role control

| Field                                       | Default | Meaning                                                                                                                                   |
| ------------------------------------------- | ------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| `controller.advisory`                       | `false` | Records proposed role splits and their reasons without changing the current roles.                                                        |
| `controller.monitor_interval_s`             | `1.0`   | Delay between monitor passes. Positive.                                                                                                   |
| `controller.monitor_failure_limit`          | `5`     | Consecutive passes with any monitoring-stage failure before the degraded state stops new admission. At least 1.                           |
| `controller.min_prefill`                    | `1`     | Minimum live prefill engines that controller moves must preserve. At least 1.                                                             |
| `controller.min_decode`                     | `1`     | Minimum live decode engines that controller moves must preserve. At least 1.                                                              |
| `controller.thresholds.expand`              | `1.0`   | SLO-relative pool load that starts reactive expansion. Positive.                                                                          |
| `controller.thresholds.shrink`              | `0.5`   | Maximum projected source load for ordinary consolidation. Nonnegative and lower than `expand`. Mixed-pressure D-to-P moves may exceed it. |
| `controller.thresholds.cooldown_s`          | `10.0`  | Minimum time between prefill-to-decode moves. Nonnegative.                                                                                |
| `controller.thresholds.sustained_intervals` | `3`     | Minimum confirmation count for nonurgent, incomplete-demand, and mixed-pressure proposals. At least 1.                                    |
| `controller.thresholds.dwell_s`             | `0.0`   | Minimum time an engine stays in a role after changing to it. Nonnegative.                                                                 |
| `controller.thresholds.panic_ratio`         | `0.0`   | Decode-load multiple that may bypass cooldown while prefill is below `shrink`. `0` disables it; enabled values must be at least 1.        |
| `controller.thresholds.flip_resident_guard` | `0`     | Maximum resident decode streams allowed on a D-to-P candidate. `0` disables the guard.                                                    |
| `controller.flip_history`                   | `1000`  | Maximum number of role-change records kept and exposed by `/narwhal/state`. At least 1.                                                   |

`recovery.health.min_samples` must not exceed `floor(recovery.health.window_s / controller.monitor_interval_s)`, because each monitor pass contributes at most one residual per engine. Even within that bound, delayed passes can leave a window undersampled.

### 7.1 Load definitions

Prefill load is predicted prefill work divided by the TTFT target.

Decode load needs a little more setup. The corrected idle floor is the profile's [zero-contention decode interval](../telemetry/02-Profiles.md#profile-fields) multiplied by the engine's bounded ratio of live to profiled decode latency. Subtracting that floor from the TPOT target gives the remaining TPOT budget. Decode load is the observed token interval above the floor divided by the remaining budget, and it never goes below zero. If the idle floor alone reaches the TPOT target, Narwhal uses the raw ratio of interval to target instead.

For either phase, a load of `1.0` means the target has been reached.

### 7.2 Role floors

`controller.min_prefill + controller.min_decode` must fit within the configured fleet. A single engine with both floors set to `1` is accepted and serves aggregate inference.

Pins, drains, quarantine, and health ejections can leave too few movable engines to satisfy a floor. When that happens, Narwhal reports the breach and keeps the safest split it can reach. No role change ever takes the fleet below `controller.min_decode`.

If live decode capacity falls below its floor, the monitor skips the normal cooldown and restores one eligible engine per pass, though it still respects each engine's dwell time. Prefill-floor recovery ignores both cooldown and dwell, but pins, engine availability, both floors, and advisory mode still apply. Every floor recovery records a dwell timestamp and counts toward the `controller.flip_history` limit like any other role change. Once capacity returns, the split goes back through the ordinary adjacent-split decision.

### 7.3 Resident work during role changes

Before turning decode capacity into prefill capacity, the controller checks three things: the proposed split stays inside the measured profile domain, the resident decode batches stay inside it too, and there is enough KV capacity to hold the resident work. Engine restarts and operator drains take an engine out of consideration by marking it unavailable.

### 7.4 Advisory rollout

Before allowing role movement in production, run Narwhal in advisory mode against recorded traffic:

```json
{
  "controller": {
    "advisory": true
  }
}
```

`/narwhal/state` and Prometheus then show the proposed prefill and decode counts along with the caller, reason, and result of each proposal.

### 7.5 Adjacent-split decisions

The controller evaluates the current split against each adjacent split. Ordinary consolidation requires projected source pressure at or below `controller.thresholds.shrink`, and it must lower the worst projected SLO ratio by at least `controller.reactive.movement_margin`.

Mixed pressure is the exception. When measured prefill pressure reaches `controller.thresholds.expand` and every engine has a profile, the controller may move one decode engine to prefill even though projected decode pressure is still above `shrink`. The move must still improve the worst projected SLO ratio by the movement margin, or by any positive amount if the margin is zero.

A nonurgent proposal must be confirmed `max(controller.reactive.confirmations, controller.thresholds.sustained_intervals)` times in a row, and the same count applies when demand evidence is complete. Any change to the proposed split, to demand completeness, or to the eligibility rule restarts the count, and a failed eligibility check drops the candidate altogether.

Prefill-to-decode moves require prefill pressure at or below `shrink` and must respect the decode cooldown.

During overload, the movement margin, confirmations, consolidation evidence, and dwell stop roles from flipping back and forth. They do not cure the overload. Meeting SLOs under sustained overload takes less offered demand or more serving capacity.

### 7.6 Evidence gating for D-to-P consolidation

Decode-to-prefill consolidation waits for a closed arrival-evidence window. The window closes once `controller.reactive.evidence_span_s` has passed with at least `controller.reactive.evidence_min_arrivals` samples, or, when traffic is sparse, once `controller.reactive.evidence_max_span_s` has passed.

Decode demand also has to be stable. As soon as the short horizon holds at least `controller.reactive.evidence_min_arrivals` samples, consolidation pauses whenever:

```text
short_horizon_demand > long_horizon_demand * (1 + controller.reactive.demand_rise_tolerance)
```

Candidates are priced with the larger of the two demand estimates, counting both resident and pending decode work. A first-token timeout or a prefill-to-decode recovery move resets the evidence window. Moves toward decode, including emergency floor restoration, don't have to wait for the window to close. While it is still filling, predictive admission refusals count toward offered demand and attainment misses.

State and metrics show the short and long demand estimates, the state of the evidence window, and whichever gate is currently blocking movement.

### 7.7 Reactive-controller parameters

| Field                                               | Default | Meaning                                                                                                                                                                      |
| --------------------------------------------------- | ------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `controller.reactive.window_s`                      | `120.0` | Demand-estimation window. Positive.                                                                                                                                          |
| `controller.reactive.confirmations`                 | `2`     | Consecutive identical adjacent proposals required for nonurgent, incomplete-demand, and mixed-pressure moves. At least 1.                                                    |
| `controller.reactive.utilization`                   | `0.8`   | Fraction of each engine treated as usable capacity. Greater than 0 and at most 1.                                                                                            |
| `controller.reactive.min_arrivals`                  | `10`    | Minimum arrival count needed for a decision. At least 1.                                                                                                                     |
| `controller.reactive.demand_floor`                  | `0.5`   | Minimum accepted demand signal. Positive.                                                                                                                                    |
| `controller.reactive.movement_margin`               | `0.05`  | Required reduction in the worst projected SLO ratio before a move. Range `[0, 1)`.                                                                                           |
| `controller.reactive.step_s`                        | `5.0`   | Minimum interval between scheduled adjacent-split evaluations. A projected prefill TTFT breach may trigger one guarded D-to-P evaluation between scheduled passes. Positive. |
| `controller.reactive.evidence_span_s`               | `60.0`  | Minimum span of recent arrivals for D-to-P consolidation, and the short horizon for the rising-demand test. Positive and at most `evidence_max_span_s`.                      |
| `controller.reactive.evidence_max_span_s`           | `120.0` | Longest evidence duration under sparse traffic. Positive, at least `evidence_span_s`, and at most `window_s`.                                                                |
| `controller.reactive.evidence_min_arrivals`         | `10`    | Minimum samples within the evidence span before D-to-P consolidation. At least 1.                                                                                            |
| `controller.reactive.demand_rise_tolerance`         | `0.25`  | Largest accepted rise of short-horizon over long-horizon decode demand. Finite and nonnegative.                                                                              |
| `controller.reactive.decode_correction_min`         | `0.5`   | Lower bound on the live/profile decode correction. Positive.                                                                                                                 |
| `controller.reactive.decode_correction_max`         | `2.0`   | Upper bound on the live/profile decode correction. At least the minimum.                                                                                                     |
| `controller.reactive.decode_correction_alpha`       | `0.2`   | Fraction of each qualifying observation window applied to the correction. Range `(0, 1]`.                                                                                    |
| `controller.reactive.decode_correction_min_samples` | `8`     | Decode gaps a window needs before it updates the correction. At least 1.                                                                                                     |

Demand is priced with the mean profile across the configured engines. Decode profiles treat active requests and resident KV tokens as separate inputs, and recent token intervals apply a bounded correction to the profile's estimate. Every configured engine shares one hardware and TP shape, so these measurements describe the whole fleet. After changing the engine build or serving policy, repeat preflight and the deployment-load measurements.
