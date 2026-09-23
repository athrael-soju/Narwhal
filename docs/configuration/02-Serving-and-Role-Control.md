# Serving and role control

## 4. Request admission and bounded serving

Narwhal can operate with direct dispatch or with bounded admission waiting, per-phase concurrency limits, and retries.

Without bounded-serving settings, admitted work is sent directly to phase dispatch and each request receives one prefill/decode attempt.

### 4.1 Global admission

| Field                        | Default        | Meaning                                                                                                                                                                                      |
| ---------------------------- | -------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `serving.admission`          | `"predictive"` | `predictive` prices every prefill path against the TTFT target and constrains aggregate placement to the single-phase region covered by measurements. `open` disables both admission checks. |
| `serving.admission_margin`   | `0.0`          | Fraction added to the TTFT admission budget to reduce boundary churn. Nonnegative.                                                                                                           |
| `serving.max_connections`    | `512`          | Global admitted-request limit and HTTP data-pool size. At least 1.                                                                                                                           |
| `engine.control_connections` | `0`            | HTTP connections reserved for health and recovery. `0` derives two per engine, with a minimum of four. Nonnegative.                                                                          |

Predictive admission returns HTTP 429 when the least expensive prefill path exceeds the TTFT budget.

Backlog-driven refusals include `Retry-After` with the projected wait.

If a prompt cannot meet TTFT even with no backlog, Narwhal returns an error envelope directing the caller to shorten the prompt or increase the TTFT target.

Measure sustained healthy inflight load before increasing `serving.max_connections`.

The loader rejects any admission override above `serving.max_connections`, because admission beyond the dispatch pool would otherwise be possible.

### 4.2 Waiting, phase concurrency, and retries

| Field                         | Default    | Meaning                                                                                                                                                                                                         |
| ----------------------------- | ---------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `serving.queue_capacity`      | `0`        | Maximum requests waiting for admission. `0` rejects immediately at saturation.                                                                                                                                  |
| `serving.queue_timeout_s`     | `0.0`      | Maximum admission wait, capped by the original request deadline. Must be positive when queueing is enabled.                                                                                                     |
| `serving.prefill_concurrency` | `0`        | Resident prefill requests per engine. Queueing requires a positive measured value.                                                                                                                              |
| `serving.decode_concurrency`  | `0`        | Resident decode requests per engine. Queueing requires a positive measured value.                                                                                                                               |
| `serving.handoff_timeout_s`   | `0.0`      | Maximum KV-handoff age measured from the start of the prefill HTTP request. Queueing or retry requires a positive value below the verified backend lease. At `0`, only the request deadline limits handoff age. |
| `serving.max_attempts`        | `1`        | Maximum complete prefill/decode attempts per original request. Range 1 to 3.                                                                                                                                    |
| `serving.retry_base_s`        | `0.1`      | Initial exponential-backoff ceiling. Full jitter samples from zero to this ceiling.                                                                                                                             |
| `serving.retry_cap_s`         | `1.0`      | Maximum backoff ceiling. At least the base value.                                                                                                                                                               |
| `serving.retry_budget`        | `10`       | Initial and maximum retry-credit pool. Each retry spends one credit.                                                                                                                                            |
| `serving.retry_replenish`     | `0.1`      | Credits added after each successful original request. Range zero to one.                                                                                                                                        |
| `serving.max_request_bytes`   | `4194304`  | Maximum HTTP request-body size, enforced while reading.                                                                                                                                                         |
| `serving.max_response_bytes`  | `16777216` | Maximum retained bytes for each non-streaming attempt and the streaming pre-output metadata buffer.                                                                                                             |

While Narwhal is reading request bodies, waiting for admission, or writing responses, it retains at most:

```text
serving.max_connections + serving.queue_capacity
```

completion requests.

Reaching this ceiling returns HTTP 429 before body parsing. The request is recorded as an unsized offer.

All active requests share this single global limit.

Queueing and phase-dispatch waits still contribute to demand. Retries preserve the original arrival time, deadline, and reservation.

Before any visible output, Narwhal may retry:

- transient transport failures
- HTTP 408
- HTTP 429
- HTTP 500
- HTTP 502
- HTTP 503
- HTTP 504

Every retry begins with fresh prefill ownership. Recovery from an expired handoff does the same.

A permanent error, local HTTP-pool starvation, cancellation, or any failure after visible output terminates the request.

For non-streaming output, partial response bodies from failed attempts are discarded before retry.

The original request deadline covers:

- tokenisation
- queue wait
- retry backoff
- engine work
- client writes

Set queue capacity, phase concurrency, and deadlines from measured workload latency and capacity. Byte limits bound router-retained data.

Set `serving.handoff_timeout_s` below the producer's KV lease. Verify separately that abandoned handoffs are released by the backend when the lease expires.

Queueing, concurrency limits, handoff expiry, retries, and byte limits change deployment capacity. Repeat workload measurement after changing any of them.

### 4.3 Streaming failure semantics

Prefill failures and non-streaming decode failures return HTTP errors.

Streaming responses commit HTTP 200 before decode starts. A later decode failure is therefore reported as a terminal stream error event, including a failure that occurs before the first generated token.

When the overall request deadline expires, Narwhal emits:

```text
code: expired
```

and closes the stream.

Client backpressure closes the connection immediately.

A client should treat either condition as a failed response:

- an error event
- stream termination before the success terminator

Any client-side retry must fit inside the caller's remaining deadline.

---

## 5. Placement

Placement filters engines in this order of eligibility:

- role
- availability
- exclusions
- projected SLO compliance

Narwhal then selects the lowest-cost eligible engine. Equal-cost candidates are resolved deterministically by instance ID.

If every candidate violates its projected SLO, Narwhal records an unserved placement and selects the least-cost fallback.

Engine-side prefix caching is independent of router placement.

A live role change affects new requests immediately. Resident requests stay on their current engine until completion or cancellation.

Lifecycle drains, quarantine, ejection, and restart holds remain active while roles change.

If admitted work exceeds what engines can drain before KV handoffs expire, decode may fail even though admission succeeded.

---

## 6. Request deadlines and engine HTTP behaviour

| Field                           | Default                | Meaning                                                                                                                                 |
| ------------------------------- | ---------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `profiles.path`                 | `"runs/profiles.json"` | Profile store read by the router and written by `narwhal-profile`.                                                                      |
| `serving.request_timeout_s`     | `600.0`                | End-to-end completion deadline from HTTP ingress through response delivery. Positive.                                                   |
| `serving.prefill_timeout_s`     | `120.0`                | Prefill-leg deadline. Positive.                                                                                                         |
| `recovery.failure_quarantine_s` | `0.0`                  | Time a failed engine remains excluded from placement. `0` disables quarantine.                                                          |
| `engine.decode_read_timeout_s`  | `60.0`                 | Maximum silent interval between decode chunks. `0` disables the gap limit.                                                              |
| `engine.first_token_timeout_s`  | `2.5`                  | Deadline to the first decode token and for each functional-verification leg. Positive.                                                  |
| `engine.tokenize`               | `true`                 | Requests exact input length from the dialect tokenisation endpoint.                                                                     |
| `engine.tokenize_timeout_s`     | `2.0`                  | Exact-token-count deadline. Positive.                                                                                                   |
| `engine.chars_per_token`        | `3.8`                  | Character-to-token fallback ratio. Positive.                                                                                            |
| `engine.pool_timeout_s`         | `5.0`                  | Maximum wait for an engine HTTP connection on serving or control pools. Probe exhaustion leaves the engine verdict unchanged. Positive. |
| `engine.connect_timeout_s`      | `10.0`                 | TCP-connect deadline for engine requests. Positive.                                                                                     |
| `engine.health_timeout_s`       | `5.0`                  | Deadline for preflight, breaker, and readmission health probes. Positive.                                                               |

Set `engine.first_token_timeout_s` above measured crossed-handoff p99 over the fleet's supported context range.

`serving.request_timeout_s` bounds the entire request, including decode streaming.

`engine.first_token_timeout_s` starts when the decode HTTP stream opens and ends when the first generated token arrives.

After the first token, `engine.decode_read_timeout_s` bounds the silent gap between transport chunks. Partial SSE lines and metadata chunks reset that timer.

With:

```json
{
  "engine": {
    "decode_read_timeout_s": 0
  }
}
```

the overall request deadline becomes the only stream bound.

Breaker verification applies `engine.first_token_timeout_s` independently to each complete prefill and decode verification leg.

`engine.chars_per_token` feeds the quadratic prefill estimate when exact tokenisation is unavailable. Profile this ratio for every dialect that can fall back to character-based estimation.

---

## 7. Role control

The controller can operate in advisory mode or apply role movements directly.

| Field                                       | Default | Meaning                                                                                                                                   |
| ------------------------------------------- | ------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| `controller.advisory`                       | `false` | Records proposed role splits and reasons without changing roles.                                                                          |
| `controller.monitor_interval_s`             | `1.0`   | Delay between monitor passes. Positive.                                                                                                   |
| `controller.monitor_failure_limit`          | `5`     | Consecutive passes with any monitoring-stage failure before degraded state stops new admission. At least 1.                               |
| `controller.min_prefill`                    | `1`     | Minimum live prefill engines preserved by controller moves. At least 1.                                                                   |
| `controller.min_decode`                     | `1`     | Minimum live decode engines preserved by controller moves. At least 1.                                                                    |
| `controller.thresholds.expand`              | `1.0`   | SLO-relative pool load that initiates reactive expansion. Positive.                                                                       |
| `controller.thresholds.shrink`              | `0.5`   | Maximum projected source load for ordinary consolidation. Nonnegative and lower than `expand`. Mixed-pressure D-to-P moves may exceed it. |
| `controller.thresholds.cooldown_s`          | `10.0`  | Minimum time between prefill-to-decode moves. Nonnegative.                                                                                |
| `controller.thresholds.sustained_intervals` | `3`     | Minimum confirmation count for nonurgent, incomplete-demand, and mixed-pressure proposals. At least 1.                                    |
| `controller.thresholds.dwell_s`             | `0.0`   | Minimum residence time after an engine changes role. Nonnegative.                                                                         |
| `controller.thresholds.panic_ratio`         | `0.0`   | Decode-load multiple that may bypass cooldown while prefill is below `shrink`. `0` disables it; enabled values must be at least 1.        |
| `controller.thresholds.flip_resident_guard` | `0`     | Maximum resident decode streams allowed on a D-to-P candidate. `0` disables the guard.                                                    |
| `controller.flip_history`                   | `1000`  | Maximum retained role-change records exposed by `/narwhal/state`. At least 1.                                                             |

`recovery.health.min_samples` cannot exceed:

```text
floor(recovery.health.window_s / controller.monitor_interval_s)
```

Each monitor pass contributes at most one residual per engine, so delayed passes can undersample a window.

### 7.1 Load definitions

Prefill load is:

```text
predicted prefill work / TTFT target
```

Decode load uses the observed token interval above the corrected idle floor, divided by the remaining TPOT budget.

If the idle floor itself reaches the TPOT target, Narwhal uses the raw interval-to-target ratio.

A load of `1.0` means the phase target has been reached.

### 7.2 Role floors

`controller.min_prefill + controller.min_decode` must fit the configured fleet.

A single-engine topology with both floors set to `1` is accepted and serves aggregate inference.

Pins, drains, quarantine, and health ejections can leave too few movable engines to satisfy a configured floor. Narwhal reports the breach and keeps the safest reachable split.

Every role change preserves `controller.min_decode`.

If live decode capacity falls below its floor, the monitor skips normal cooldown and restores one eligible engine per pass. Decode-floor recovery still observes per-engine dwell.

Prefill-floor recovery ignores cooldown and dwell. Pins, engine availability, both floor constraints, and advisory mode still apply.

Every applied floor recovery records a dwell timestamp.

When capacity returns, the resulting split re-enters the ordinary adjacent-split decision path.

Recovery changes are retained under the same `controller.flip_history` limit as ordinary role changes.

### 7.3 Resident work during role changes

New requests observe a role change immediately.

Existing requests retain their current engines and reservations until completion or cancellation.

Before turning decode capacity into prefill capacity, the controller verifies that:

- the proposed split remains inside the measured profile domain,
- resident decode batches remain inside the measured profile domain,
- available KV capacity can hold the resident work.

Engine restart and operator drain mark the engine unavailable.

### 7.4 Advisory rollout

Run:

```json
{
  "controller": {
    "advisory": true
  }
}
```

against recorded traffic before enabling production role movement.

`/narwhal/state` and Prometheus expose proposed prefill/decode counts, caller, reason, and result.

### 7.5 Adjacent-split decisions

The controller evaluates the current split and each adjacent split.

Ordinary consolidation requires both:

- projected source pressure at or below `controller.thresholds.shrink`
- reduction in the worst projected SLO ratio of at least `controller.reactive.movement_margin`

When measured prefill pressure reaches `controller.thresholds.expand`, and every engine has a profile, the mixed-pressure rule may move one decode engine to prefill even if projected decode pressure remains above `shrink`.

That move still must improve the worst projected SLO ratio by at least the configured movement margin. If the margin is zero, the improvement must remain strictly positive.

The number of matching confirmations required is:

```text
max(
  controller.reactive.confirmations,
  controller.thresholds.sustained_intervals
)
```

This rule also applies when demand evidence is complete.

Any change in:

- proposed split
- demand completeness
- eligibility rule

resets the confirmation sequence. A failed eligibility check clears the candidate.

Prefill-to-decode moves require prefill pressure at or below `shrink` and obey the decode cooldown.

During overload, movement margin, confirmations, consolidation evidence, and dwell limit repeated role changes. Sustained overload still requires reduced offered demand or more serving capacity to meet SLOs.

### 7.6 Evidence gating for D-to-P consolidation

Decode-to-prefill consolidation requires a closed arrival-evidence window.

The window closes when either condition is met:

- `controller.reactive.evidence_span_s` has elapsed and at least `controller.reactive.evidence_min_arrivals` samples exist
- `controller.reactive.evidence_max_span_s` has elapsed under sparse traffic

Decode demand must also be stable.

Consolidation pauses when the short-horizon estimate exceeds the long-horizon estimate by more than `controller.reactive.demand_rise_tolerance`.

Candidate pricing uses the larger demand estimate and includes resident plus pending decode work.

A first-token timeout or prefill-to-decode recovery move resets the consolidation evidence window.

Moves toward decode, including emergency floor restoration, may proceed while evidence is still accumulating.

Predictive admission refusals contribute to offered demand and attainment misses without resetting the evidence window.

State and metrics expose:

- short and long demand estimates
- evidence-window state
- the gate currently blocking movement

### 7.7 Reactive-controller parameters

| Field                                               | Default | Meaning                                                                                                                                                                      |
| --------------------------------------------------- | ------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `controller.reactive.window_s`                      | `120.0` | Demand-estimation window. Positive.                                                                                                                                          |
| `controller.reactive.confirmations`                 | `2`     | Required consecutive identical adjacent proposals for nonurgent, incomplete-demand, and mixed-pressure moves. At least 1.                                                    |
| `controller.reactive.utilization`                   | `0.8`   | Fraction of each engine treated as usable capacity. Greater than 0 and at most 1.                                                                                            |
| `controller.reactive.min_arrivals`                  | `10`    | Minimum arrival count required for a decision. At least 1.                                                                                                                   |
| `controller.reactive.demand_floor`                  | `0.5`   | Minimum accepted demand signal. Positive.                                                                                                                                    |
| `controller.reactive.movement_margin`               | `0.05`  | Required reduction in worst projected SLO ratio before movement. Range `[0, 1)`.                                                                                             |
| `controller.reactive.step_s`                        | `5.0`   | Minimum interval between scheduled adjacent-split evaluations. A projected prefill TTFT breach may trigger one guarded D-to-P evaluation between scheduled passes. Positive. |
| `controller.reactive.evidence_span_s`               | `60.0`  | Minimum recent-arrival span required for D-to-P consolidation and short horizon for the rising-demand test. Positive and no greater than `evidence_max_span_s`.              |
| `controller.reactive.evidence_max_span_s`           | `120.0` | Maximum evidence duration under sparse traffic. Positive, at least `evidence_span_s`, and no greater than `window_s`.                                                        |
| `controller.reactive.evidence_min_arrivals`         | `10`    | Minimum samples within the evidence span before D-to-P consolidation. At least 1.                                                                                            |
| `controller.reactive.demand_rise_tolerance`         | `0.25`  | Maximum accepted short-horizon rise over long-horizon decode demand. Finite and nonnegative.                                                                                 |
| `controller.reactive.decode_correction_min`         | `0.5`   | Lower bound on live/profile decode correction. Positive.                                                                                                                     |
| `controller.reactive.decode_correction_max`         | `2.0`   | Upper bound on live/profile decode correction. At least the minimum.                                                                                                         |
| `controller.reactive.decode_correction_alpha`       | `0.2`   | Fraction of each qualifying observation window applied to the correction. Range `(0, 1]`.                                                                                    |
| `controller.reactive.decode_correction_min_samples` | `8`     | Required decode gaps before a window updates the correction. At least 1.                                                                                                     |

Consolidation is blocked when short-horizon demand exceeds long-horizon demand by more than:

```text
1 + controller.reactive.demand_rise_tolerance
```

Demand pricing uses the mean profile across configured engines.

Decode profiles model active requests and resident KV tokens as separate inputs. Recent token intervals apply a bounded correction to the profile estimate.

All configured engines share one hardware and TP shape, so these measurements describe the full fleet.

Repeat preflight and deployment-load measurements after changing engine build or serving policy.
