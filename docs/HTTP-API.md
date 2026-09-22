# HTTP API reference

Narwhal exposes two completion endpoints, seven inspection endpoints, and two lifecycle actions. FastAPI publishes typed schemas at `/docs` and `/openapi.json`.

## Public names

Narwhal v0.1.0 uses the following public interfaces:

| Interface                      | Namespace                                                                  |
| ------------------------------ | -------------------------------------------------------------------------- |
| Python distribution and import | `narwhal-inference`, `narwhal`                                             |
| Operator commands              | `narwhal-*`                                                                |
| Completion API                 | `/v1/completions`, `/v1/chat/completions`                                  |
| Router control API             | `/narwhal/state`, `/narwhal/handoff`, `/narwhal/lifecycle` and its actions |
| Health, readiness and metrics  | `/health`, `/ready`, `/metrics`                                            |
| Router telemetry               | `narwhal_*`                                                                |
| Persisted schema identifiers   | `narwhal.*`, versioned per document                                        |

`narwhal_contract_info` identifies metrics contract version 1. Cite Arrow research according to [CITATION.cff](https://github.com/athrael-soju/Narwhal/blob/main/CITATION.cff).

## Completion routes

### `POST /v1/completions`

Accepts an OpenAI completions request. A non-streaming response has `object: "text_completion"` and returns generated text in `choices[0].text`.

### `POST /v1/chat/completions`

Accepts an OpenAI chat completions request. A non-streaming response has `object: "chat.completion"` and returns `choices[0].message` with `role: "assistant"`. If the response contains only reasoning or tool-call output, `content` is null.

### Response compatibility

For non-streaming clients, Narwhal consumes the engine stream and assembles a single response. Engine fields are preserved as follows:

| Output                                                      | Non-streaming assembly                                                                                                         |
| ----------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| Chat `content`, `reasoning`, `reasoning_content`, `refusal` | String deltas concatenate under the original message field.                                                                    |
| Function `tool_calls`                                       | Calls are grouped by stream index and returned in index order. ID, function name and arguments are concatenated for each call. |
| Legacy `function_call`                                      | Name and argument fragments concatenate into one message field.                                                                |
| Logprobs                                                    | Chat content/refusal arrays and text-completion token, logprob and offset arrays concatenate in stream order.                  |

Every tool call requires an ID and function name. Arguments remain engine-generated strings for the client to interpret. Tool, reasoning and input-format support depends on the configured model and engine.

Non-streaming requests support text output and function tools. Narwhal returns `400` before dispatching engine work when a request asks for `audio`, an output `modalities` value other than `["text"]`, or a tool type other than `function`.

Response assembly accepts a fixed set of choice and chat-delta fields. Any other non-null field, including audio, annotations or custom tool output, causes the request to fail with `502`. Malformed values in otherwise accepted fields fail the request as well.

Streaming responses preserve the engine's delta fields and shape, subject to the token-ID exposure rules below. Stream termination and serving limits apply to both streaming and non-streaming paths.

Narwhal constructs engine requests from the submitted body. It first validates the requested model name, then replaces `model` with the configured served model. Only one sequence is supported per request. Values of `n` or `best_of` above 1 return `400`, keeping prefill and decode sampling widths aligned.

Both completion endpoints require a JSON object. Non-null values for router-interpreted fields must have these types:

| Field                        | Required type                  |
| ---------------------------- | ------------------------------ |
| `model`                      | String                         |
| `stream`                     | Boolean                        |
| `n`, `best_of`, `max_tokens` | Integer; booleans are rejected |
| `prompt`                     | String or array                |
| `messages`                   | Array of objects               |

Invalid JSON, an invalid body shape, or an invalid field type returns `400` in an OpenAI error envelope. Diagnostics identify the field and violated rule.

```json
{"error": {"message": "max_tokens must be an integer", "type": "invalid_request_error", "param": "max_tokens", "code": null}}
```

Validation failures record one invalid terminal outcome before Narwhal reserves admission or engine capacity. Fields outside the router validation set pass through unchanged.

Narwhal assigns `x-request-id` when the request reaches the router. Each engine attempt and phase receives a separate ID for KV ownership. The journal stores the original client ID as `client_rid`, preserving end-to-end traceability.

Ingress owns client authentication and removes client credentials before forwarding. Set `engine.engine_api_key_env` to attach the deployment engine credential to all serving and control requests. [Configure Narwhal](Configuration.md#engine-authentication) documents the authentication boundary.

Ingress should also strip client-supplied internal credentials and request IDs before installing trusted replacements, as described in [Operate Narwhal](Operate.md#configure-ingress). Narwhal derives client identity from those trusted values.

### Admission

| Condition                                                                              | Status | Response detail                                                      |
| -------------------------------------------------------------------------------------- | -----: | -------------------------------------------------------------------- |
| Invalid JSON, body shape or router-interpreted field type                              |  `400` | `invalid_request_error` with the field in `param`                    |
| Requested model differs from `model`                                                   |  `404` | Error code `model_not_found`                                         |
| `n` or `best_of` exceeds 1                                                             |  `400` | Invalid sampling width                                               |
| Non-streaming request asks for audio, non-text output modalities or non-function tools |  `400` | `invalid_request_error` naming the rejected option in `param`        |
| HTTP retention limit or admission queue is full                                        |  `429` | `retry-after: 1`                                                     |
| Request body exceeds `serving.max_request_bytes`                                       |  `413` | `request_too_large`                                                  |
| Admission wait or original deadline expires before headers                             |  `504` | Terminal expiry                                                      |
| Predictive admission prices the queue above the TTFT budget                            |  `429` | `retry-after` contains the rounded queue overrun                     |
| Prompt alone exceeds the TTFT budget                                                   |  `429` | Error envelope only; shorten the prompt or raise the target          |
| All engines are excluded from placement                                                |  `503` | `backend_unavailable` with `retry-after: 1`                          |
| Router is standby, fenced, in whole-wave maintenance or monitoring-degraded            |  `503` | Retryable refusal with `retry-after: 1`; `/ready` reports the reason |

Global counters classify capacity rejections as `rejected`, predictive refusals as `refused`, and malformed bodies as `invalid_requests`.

Every request reaching a supported completion endpoint increments `offered` once. If the body terminates before Narwhal can size the workload, the request also increments `unsized_offered`.

Set `admission` to `open` to disable predictive refusal. Concurrency limits remain active.

### Backend continuation contract

The shipping vLLM/NIXL path sends the producer a non-streaming completion request for one token. Narwhal discards that generated token. `PrefillResult` associates the backend-owned KV descriptor with the producer URL, endpoint and request ID; decode then resumes from the original prompt.

Remote decode receives the original prompt or messages, requested output limit, sampling settings and validated descriptor. Decode produces every client-visible output token. Same-worker decode removes transfer parameters, including client-provided values, and relies on engine prefix caching or prompt recomputation.

Descriptor validation runs before the decode HTTP request. Narwhal rejects missing engine identity, malformed block IDs, and connector or endpoint mismatches while retaining opaque runtime fields. Preflight and occupied-role canaries verify transfer correctness against the pinned engine contract.

The original request lifecycle owns the handoff-age limit, phase reservations, retries and cleanup. Handoff age begins when the producer HTTP leg starts. Every retry receives fresh backend request IDs and new producer ownership. Producer HTTP completion, first visible decode output and the handoff interval between them are measured separately.

Python callers import `EngineClient` from `narwhal.engines.client` and `PrefillResult` from `narwhal.engines.connector`. Pass the object returned by `EngineClient.prefill()` directly to `EngineClient.decode()`. `result.parameters()` returns a detached dictionary for inspection. Internal Python APIs may change between releases.

### Engine failures

A failed engine leg returns `504` for timeout-shaped faults and `502` for other engine faults. Prefill fails before streaming begins, so Narwhal can return its HTTP error status directly.

In fleets configured with an `engine_contract`, an ejected engine must pass lifecycle validation before breaker readmission. Development fleets without a contract recover on health checks alone.

After `recovery.eject_after` consecutive stream failures, Narwhal removes an engine from placement until it passes an inference probe. First-token timeouts and mid-stream silence count toward the streak. After a crossed-decode failure, the probe uses a fresh handoff generated by the original producer. Each probe leg uses `engine.first_token_timeout_s`; inconclusive probes return on the normal readmission cadence. Whole-wave restart policy still applies.

A successful engine stream contains generated output followed by `data: [DONE]`. Closing before `[DONE]` is an engine failure. Receiving `[DONE]` before the first generated token returns `502` with:

`stream ended with [DONE] before any token arrived`

Error objects carried inside an upstream HTTP 200 stream propagate with their own status.

`engine.first_token_timeout_s` limits the interval from opening the decode stream to receiving the first generated token.

After the first token, `engine.decode_read_timeout_s` limits silence between transport chunks. Metadata chunks reset the timer. Expiry returns `504` with detail beginning `engine went silent between tokens`. A value of zero uses the original request deadline as the stream bound.

Each admitted request receives one prefill/decode attempt by default. Before visible output, transient faults may trigger a fresh attempt if both the original request deadline and retry budget allow it. `recovery.failure_quarantine_s > 0` temporarily removes the failed engine from later placement while the breaker state catches up. A decode failure after streaming has committed HTTP 200 emits a terminal `data: {"error": ...}` event.

For engines that expose token IDs, Narwhal adds `return_token_ids: true` and `stream_interval: 1` to decode requests. Identified tokens are counted across text, reasoning and tool-call output.

Valid token IDs are nonnegative integers; booleans are invalid. Serving and profiling paths require valid token identity for text, reasoning, tool-call and refusal output whenever they perform exact counting. Invalid identity fails the decode attempt or measurement.

The `token_ids` accounting dialect provides exact token identity for output length and TPOT scoring. All other dialects report `unavailable`. Decode correction, drift scoring and output-length learning require identified tokens.

Clients receive token IDs in both streaming and non-streaming responses when they request `return_token_ids`.

Non-streaming assembly retains response metadata and non-null engine `usage` values across metadata-only frames. If the engine omits usage, Narwhal calculates it from input length and measured output tokens. `finish_reason` and optional `stop_reason` come from the engine and survive later usage-only frames.

## Inspection routes

### `GET /v1/models`

Returns the configured model in OpenAI list format.

```json
{
  "object": "list",
  "data": [
    {"id": "example/model", "object": "model", "owned_by": "narwhal"}
  ]
}
```

### `GET /health`

Reports process liveness as `ok`, `standby`, `fenced`, `maintenance` or `degraded`, together with configured fleet size and the number of placement-eligible engines.

```json
{"status": "ok", "instances": 6, "available_instances": 6}
```

All health states return HTTP 200. `available_instances` counts engines currently eligible for placement after applying the router's latest ejection, drain and quarantine state. Prometheus scrape targets and breaker state provide engine-level liveness.

### `GET /ready`

Returns HTTP 200 while the router owns control and admits requests. Standby state, fencing, lease-storage failure, lifecycle holds, backend loss and monitoring degradation return HTTP 503 with a reason. Load balancers should use this endpoint.

After `controller.monitor_failure_limit` consecutive failed monitoring passes, readiness reports `monitoring degraded: <stage> <class>`. Standbys count the same failures toward takeover. One fully successful monitoring pass clears the degraded state.

If placement has no eligible engine, `/ready` returns HTTP 503 with `reason: no available engines`. New completion requests receive `backend_unavailable` and `Retry-After: 1`.

Lifecycle and control holds take precedence over backend state. During a whole-wave hold, `/health` reports `maintenance`, `/ready` reports the lifecycle reason, and completion requests return HTTP 503 with error code `standby`.

`control_ready` remains true during backend loss or managed maintenance when the router still owns its lease and monitoring is healthy. Standbys use this state to keep handoffs current. Client traffic belongs only on routers where `/ready` returns HTTP 200.

### `GET /metrics`

Returns Prometheus exposition format 0.0.4. [Metrics](Telemetry-and-Artifacts.md#metrics) lists the operational series.

### `GET /narwhal/handoff`

Returns a fresh control-plane handoff.

| Field               | Meaning                                                                                                                                                                         |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `schema`            | `narwhal.handoff`                                                                                                                                                               |
| `schema_version`    | Handoff schema version; this release writes `1`                                                                                                                                 |
| `at`                | Unix wall-clock timestamp of the snapshot                                                                                                                                       |
| `run`               | Request-journal run ID of the writing process                                                                                                                                   |
| `model`             | Configured served model                                                                                                                                                         |
| `epoch`             | Lease epoch at capture time; zero when HA fencing is disabled                                                                                                                   |
| `holder`            | Unique lease-holder token; empty when HA fencing is disabled                                                                                                                    |
| `engines`           | Sorted configured engine IDs                                                                                                                                                    |
| `roles`             | Engine ID mapped to current `prefill` or `decode` role                                                                                                                          |
| `ejected`           | Engine IDs currently held out by the breaker                                                                                                                                    |
| `inference_sources` | Suspect engine IDs mapped to producer IDs required for verification; an empty producer ID requests a local probe                                                                |
| `counters`          | `served`, `failed`, `unserved`, `refused`, `rejected` and `cancelled` totals                                                                                                    |
| `lifecycle`         | Durable drain, validation, whole-wave state, restart policy and accepted process starts                                                                                         |
| `demand_risk`       | Latest consolidation-risk event: `kind`, elapsed `age_s`, and per-kind counts; the receiver reanchors age to its own clock and gathers new arrival evidence; `null` means clear |

Package and Git provenance live in the journal header.

A handoff restores the persisted `served`, `failed`, `unserved`, `refused`, `rejected` and `cancelled` totals. The replacement process starts fresh resident tracking, flip history, role-change and controller-decision counters, latency histograms, floor history and monitoring-failure counters.

Narwhal writes the handoff to `recovery.state_path` for `narwhal-serve --resume`. Warm standbys fetch it through this endpoint.

Expose `/narwhal/handoff` only on the trusted control network. It contains resident state, counters and lifecycle data used for standby takeover and operator tooling.

### `GET /narwhal/state`

Returns the live scheduler state as `narwhal.state` schema version 1.

| Field                                   | Meaning                                                                                                                                                                                                                                                                                                                             |
| --------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `schema`                                | `narwhal.state`                                                                                                                                                                                                                                                                                                                     |
| `schema_version`                        | State schema version; this release writes `1`                                                                                                                                                                                                                                                                                       |
| `served`, `failed`                      | Completed requests and requests ending in error, preserved across resume and takeover                                                                                                                                                                                                                                               |
| `offered`, `unsized_offered`, `expired` | Completion arrivals, arrivals terminating before sizing, and deadline expiries in this router process                                                                                                                                                                                                                               |
| `cancelled`                             | Client disconnects, recorded as their own terminal outcome and preserved across resume and takeover; `served` and `failed` retain separate totals                                                                                                                                                                                   |
| `invalid_requests`                      | Malformed client requests rejected before admission during the current run                                                                                                                                                                                                                                                          |
| `controller`                            | Active role controller; always `reactive`                                                                                                                                                                                                                                                                                           |
| `token_accounting`                      | `token_ids` for exact per-token decode identity, otherwise `unavailable`                                                                                                                                                                                                                                                            |
| `control`                               | Advisory mode, latest decision, process-lifetime decision totals and role-change totals; `flips` keys use `<caller>:<target-role>`; `flip_reversals`, `flips_refused` and `flip_inflight` record reversals, rejected changes and resident work at applied moves                                                                     |
| `monitoring`                            | Monitoring-loop timing and failure state: current and high-water event-loop lag, degraded admission gate, first failure in the current streak as `<class>:<stage>`, `core_consecutive`, lifetime `core_failures`, and per-stage failures, streaks, last class and timestamp                                                         |
| `ha`                                    | Readiness, standby state, lease epoch and holder, plus any fencing reason                                                                                                                                                                                                                                                           |
| `lifecycle`                             | Engine drain state, resident work, process identities, validation results and retained events                                                                                                                                                                                                                                       |
| `admission`                             | Active and queued occupancy, phase waiters, limits, rejections and predictive refusals                                                                                                                                                                                                                                              |
| `serving`                               | Retained HTTP work, attempt counts, retry budget, observed decode tokens and upstream time                                                                                                                                                                                                                                          |
| `http_pools`                            | Bounded engine data pool, reserved control pool and pool-wait timeout                                                                                                                                                                                                                                                               |
| `pools`                                 | Engine IDs grouped by current prefill or decode role                                                                                                                                                                                                                                                                                |
| `load`                                  | Per-pool SLO-relative load; `1.0` equals the configured target                                                                                                                                                                                                                                                                      |
| `thresholds`                            | Active reactive-controller thresholds                                                                                                                                                                                                                                                                                               |
| `slo`                                   | TTFT and TPOT targets used by placement and control                                                                                                                                                                                                                                                                                 |
| `first_token_timeout_s`                 | Decode first-token deadline                                                                                                                                                                                                                                                                                                         |
| `resident`                              | In-flight prefill and decode counts by engine                                                                                                                                                                                                                                                                                       |
| `pinned`                                | Engines excluded from role changes                                                                                                                                                                                                                                                                                                  |
| `min_prefill`                           | Configured minimum live prefill count                                                                                                                                                                                                                                                                                               |
| `min_decode`                            | Configured minimum live decode count                                                                                                                                                                                                                                                                                                |
| `below_floor`                           | Current and cumulative prefill-floor breach state                                                                                                                                                                                                                                                                                   |
| `ejected`                               | Engines removed from scheduling by the breaker                                                                                                                                                                                                                                                                                      |
| `draining`                              | Engines excluded by an operator lifecycle action                                                                                                                                                                                                                                                                                    |
| `probation`                             | Engines carrying a predictive-health placement penalty                                                                                                                                                                                                                                                                              |
| `health`                                | Per-engine drift-window accounting: scored and undersampled closed windows with observations, plus `last_scored_s_ago`; `prefill_paused` marks evidence suspended because of local prefill interference and `prefill_pauses` counts entries into that state; confirmed ejection removes the record, so readmission starts from zero |
| `quarantined`                           | Engines temporarily excluded after an engine failure                                                                                                                                                                                                                                                                                |
| `breaker`                               | Per-engine consecutive failure streaks by class: `connection`, `timeout`, `overload`, `inference_status`, `kv_handoff`, `stream`, and `liveness`; also records engines with a health or inference probe in flight                                                                                                                   |
| `decode_floor`                          | Live decode count, configured minimum, deficit state and cumulative restorations                                                                                                                                                                                                                                                    |
| `attainment`                            | Bounded diagnostic SLO outcome buckets                                                                                                                                                                                                                                                                                              |
| `demand_history`                        | Retained sized and unsized demand, shape counts and overflow bounds                                                                                                                                                                                                                                                                 |
| `demand_evidence`                       | Consolidation evidence for every D-to-P gate: retained span and sample counts, required minima, bounded lookback, closure state, short and long decode estimates, trend ratio, conservative envelope, armed risk event and per-kind counts                                                                                          |
| `unserved`                              | Phase placements where every eligible candidate exceeded the configured SLO                                                                                                                                                                                                                                                         |
| `panic_bypasses`                        | Prefill-to-decode moves allowed through cooldown by the panic condition                                                                                                                                                                                                                                                             |
| `flips_refused`                         | 20 most recent role-change refusals                                                                                                                                                                                                                                                                                                 |
| `flips`                                 | Role changes retained up to `flip_history`                                                                                                                                                                                                                                                                                          |

The nested `admission` and `serving` objects expose the following fields:

| Object      | Field                                                              | Meaning                                                                                 |
| ----------- | ------------------------------------------------------------------ | --------------------------------------------------------------------------------------- |
| `admission` | `inflight`                                                         | Requests holding a router admission seat                                                |
| `admission` | `queued`, `queue_capacity`, `queue_high_water`                     | Current queue depth, configured queue bound and peak waiters                            |
| `admission` | `waiting_prefill`, `waiting_decode`                                | Requests waiting for phase dispatch                                                     |
| `admission` | `limit`                                                            | Effective router limit after `--max-concurrent` precedence                              |
| `admission` | `rejected`                                                         | Global capacity rejections                                                              |
| `admission` | `refused`                                                          | Global predictive-admission refusals                                                    |
| `admission` | `engine_auth`                                                      | Engine authentication mode: `boundary` or `engine-credential`                           |
| `serving`   | `http_retained`, `http_retained_limit`, `http_retained_high_water` | Completion requests holding HTTP resources, their limit and peak occupancy              |
| `serving`   | `prefill_attempts`, `decode_attempts`, `retry_attempts`            | Cumulative phase dispatches and additional prefill attempts                             |
| `serving`   | `retry_credits`, `retry_credits_spent`, `retry_denied`             | Available shared retry credit, consumed credit and denied retries                       |
| `serving`   | `decode_tokens_observed`, `upstream_seconds`                       | Observed decode tokens and cumulative HTTP leg time by phase, including failed attempts |

`pools`, `load`, `resident` and controller records use these shapes:

| Object                  | Fields                                                                                                                                                                                                                                                       |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `pools`                 | `prefill`, `decode` arrays of engine IDs                                                                                                                                                                                                                     |
| `http_pools`            | `data_connections`, `control_connections`, `pool_timeout_s`                                                                                                                                                                                                  |
| `load`                  | `prefill`, `decode` SLO-relative floats                                                                                                                                                                                                                      |
| `thresholds`            | `expand`, `shrink`, `cooldown_s`, `sustained_intervals`, `dwell_s`, `panic_ratio`                                                                                                                                                                            |
| `slo`                   | `ttft_s`, `tpot_s`                                                                                                                                                                                                                                           |
| `resident.<iid>`        | `prefill`, `decode` in-flight counts                                                                                                                                                                                                                         |
| `below_floor`           | `active`, `live_prefill`, `since`, `breaches`, `cumulative_s`                                                                                                                                                                                                |
| `attainment`            | `bucket_s`, `retained_s`, `covered_s`, `buckets`, `outcomes`, `pruned_buckets`, `pruned_outcomes`                                                                                                                                                            |
| `demand_evidence`       | `span_s`, `arrivals`, `required_span_s`, `required_arrivals`, `max_span_s`, `closed`, `risk_kind`, `risk_age_s`, `risk_events`, `short_decode_engines`, `long_decode_engines`, `trend_ratio`, `envelope_decode_engines`, `blocked_gate`                      |
| `control`               | `advisory`, `last_decision`, cumulative `decisions` by caller and result                                                                                                                                                                                     |
| `control.last_decision` | Current and proposed split, demand reference, phase work, projected SLO ratios, applied decode request limit, decode profile coverage and correction, objective change, reason and result; D-to-P decisions also include the consolidation-evidence snapshot |
| `decode_floor`          | `min_decode`, `live_decode`, `below_floor`, `restoration_moves`                                                                                                                                                                                              |
| `flips_refused[]`       | `at`, requested `to` role, `why`                                                                                                                                                                                                                             |

Scored reactive decisions report `eligibility_rule` as one of three values:

- `source_shrink` for ordinary consolidation
- `mixed_pressure` when observed prefill recovery exceeds the decode shrink threshold
- `projected_ttft_recovery` for arrival-triggered D-to-P evaluation

Eligible proposals also report `confirmations` and `required_confirmations`.

Projected-TTFT recovery records:

`trigger_rid`, `projected_ttft_s`, `ttft_slo_s`, `trigger_projected_ttft_ratio`, `resident_prefill_s`, `queued_prefill_s`, `waiting_prefill`, `initial_projected_ttft_s`, `urgent_signals`, `event_to_evaluation_s`

A scored adjacent candidate adds:

`candidate_projected_ttft_s`, `candidate_projected_ttft_ratio`, `projected_ttft_improvement_s`, `decode_capacity_safe`, `role_floors_safe`, `source_pressure_safe`

For this path, `decision_basis` is `projected_ttft_recovery`. The existing `projected_ttft_ratio` continues to represent the demand model's ratio for the candidate split.

Blocked or held decisions retain their proposed split and objective change together with the constraint that stopped the move. Possible constraints include consolidation evidence, profiles, KV capacity, role floors, pins, cooldown, dwell and the resident guard. Decisions made before scoring, including insufficient demand history or fleet-health changes, contain only the fields available at that stage.

`below_floor.live_prefill` counts placement-eligible prefill engines. Role changes obey both configured floors, although health failures and operator hold-outs can push the fleet below them. The controller moves healthy decode capacity into prefill until it restores the floor or `min_decode` prevents another move.

Aggregate mode treats an initial zero-prefill pool as its baseline. `below_floor.active` remains false until the fleet has reached the configured floor at least once.

`below_floor.since` uses the process monotonic clock and is null outside a breach. While `active` is true, `below_floor.cumulative_s` includes the currently open interval.

The `attainment` record stores diagnostic SLO outcomes. Completed, failed, expired and predictively refused requests contribute TTFT-met, TPOT-met and total counts to buckets of width `monitor_interval_s`. Pruning is anchored to the newest recorded bucket, retains four demand windows, and includes the full boundary bucket in window queries.

`attainment` exposes both retained and pruned counts. `covered_s` measures the age of the oldest retained bucket, capped at the configured retention span.

A role-change record has this form:

```json
{
  "at": 1712.4,
  "iid": "e2",
  "to": "decode",
  "by": "reactive",
  "prefill_inflight": 0,
  "decode_inflight": 0,
  "drained_s": 0.0
}
```

`by` identifies the caller as `reactive`, `decode_floor` or `floor_recovery`. `prefill_inflight` and `decode_inflight` capture resident work when the role label changes. `drained_s` is filled with the drain duration when that work completes.

Narwhal writes per-request evidence to the [request journal](Telemetry-and-Artifacts.md#request-journal).

#### Demand accounting

Inside `demand_history.unsized`, `pending` counts request bodies still being read. `observations` counts retained offers that terminated before sizing. The history contains authenticated offers that passed validation.

Parsed offers enter demand history with local input-size estimates. If the request reaches admission and tokenization completes, the tokenizer result replaces the estimate at the request's original arrival time without creating a second offer. Requests rejected before tokenization keep their local estimates.

Repricing preserves bounded shape aggregation. If the last observation for a boundary moves to another cohort, that boundary evidence becomes invalid.

Demand history uses time buckets. Each bucket stores at most 128 exact request shapes plus one overflow cohort; unsized history requires only a single shape. Bucket width is the minimum of one second, the controller step and the minimum evidence span.

Arrival and residency data retain one demand window. Completed output observations retain four. Recording new evidence prunes expired buckets even when the control loop is stopped.

Overflow preserves every request count and uses the largest input and requested-output lengths in the cohort when pricing work. Uncapped overflow remains incomplete demand. Overflow in output history disables learned discounts until the affected data expires.

A cohort crossing a window boundary contributes its full count, adding at most one bucket of history. Consolidation evidence includes only observations guaranteed to fall after its cutoff.

`demand_history` and `narwhal_demand_history_*` expose retained cells, the cell limit, counted observations and overflow observations.

Each reactive decision snapshots the inputs needed to score candidate splits: profile coefficients, offered-work demand, observed phase pressure, pending output estimates and resident work in the old role. Frozen profiles and value-only split inputs prevent later live-state changes from modifying an already computed score. Output-length estimates and decode correction are built once and reused across both demand horizons.

When priced prefill waiters exist, `recovery_prefill_ratio` is the greater of:

- observed prefill pressure
- resident-plus-queued prefill seconds divided by current prefill engine count and the TTFT SLO

With zero priced prefill waiters, observed pressure supplies the ratio.

Incomplete-demand decisions expose `recovery_prefill_ratio`, `queued_prefill_s`, both observed phase ratios, and `decision_basis` set to `prefill_pressure_recovery` or `decode_pressure_recovery`. [Role control](Configuration.md#role-control) defines the movement and confirmation gates.

Consolidation checks retain full-precision demand through source-pressure evaluation and movement gating. Rounded state and journal values are output representations only. Role floors, live availability, cooldown, dwell, profile coverage and physical KV limits constrain each move.

### `GET /narwhal/lifecycle`

Returns the `narwhal.lifecycle` version 1 operator document.

`router.controls_fleet` distinguishes the active lease holder from a standby or fenced router. Each engine record exposes:

- `state`
- `draining`
- scheduler eligibility in `accepts_new`
- `ready_to_stop`
- resident prefill and decode counts
- deadline
- wave ID
- old and new process-start timestamps
- validation checks
- error state

The wave record reports whether router-wide readiness has been withdrawn and whether every member is safe for the external supervisor to stop.

Top-level `engine_restart_policy` contains the configured restart policy. `process_starts` maps engine IDs to the last accepted process-start timestamps. The top-level `error` field is empty on success and contains the rejected action's error on non-2xx responses.

### `POST /narwhal/lifecycle/drain`

Drains either one engine or the full fleet as a wave. Narwhal removes every target from placement before recording process identity. Draining the full wave also withdraws router `/ready`.

HTTP `409` rejects an unsafe request shape. HTTP `503` reports failure to capture process identity while leaving the drain hold in place.

```json
{"engines":["e0"],"deadline_s":300}
```

### `POST /narwhal/lifecycle/readmit`

Readmission verifies health, process-bound attestation, configured model, direct generation, role-compatible KV transfer and final health before releasing the hold.

A planned restart requires a process start newer than the one captured during drain. Breaker recovery may reuse the current process identity because transient faults can eject an otherwise unchanged process.

With `recovery.engine_restart_policy: whole_wave`, every lifecycle action covers members that have both a recorded drain identity and a newer process. An automatic hold does not supply those identities; operators must issue an explicit wave drain before restarting.

HTTP `409` leaves candidates that fail validation blocked.

```json
{"engines":["e0"]}
```

[Operate Narwhal](Operate.md#restart-one-engine) documents the external-supervisor sequence and whole-wave rule.

:chatgpt-content-reference{index="0"}