# API and data reference

Narwhal serves two completion routes, seven inspection routes, and two lifecycle action routes. FastAPI publishes the typed response schemas at `/docs` and `/openapi.json`.

## Public names

Narwhal v0.1.0 uses these names for its public interfaces:

| Interface                      | Namespace                                                                  |
| ------------------------------ | -------------------------------------------------------------------------- |
| Python distribution and import | `narwhal-inference`, `narwhal`                                             |
| Operator commands              | `narwhal-*`                                                                |
| Completion API                 | `/v1/completions`, `/v1/chat/completions`                                  |
| Router control API             | `/narwhal/state`, `/narwhal/handoff`, `/narwhal/lifecycle` and its actions |
| Health, readiness and metrics  | `/health`, `/ready`, `/metrics`                                            |
| Router telemetry               | `narwhal_*`                                                                |
| Persisted schema identifiers   | `narwhal.*`, versioned per document                                        |

`narwhal_contract_info` identifies metrics contract version 1; cite Arrow research following [CITATION.cff](https://github.com/athrael-soju/Narwhal/blob/main/CITATION.cff).

## Completion routes

### `POST /v1/completions`

Accepts an OpenAI completions request. Non-streaming responses use `object: "text_completion"` and `choices[0].text`.

### `POST /v1/chat/completions`

Accepts an OpenAI chat completions request. Non-streaming responses use `object: "chat.completion"` and `choices[0].message`, with `role: "assistant"`. Responses composed entirely of reasoning or tool-call output carry null content.

### Response compatibility

Narwhal folds the engine's stream for non-streaming clients. It preserves these fields when the engine emits them:

| Output                                                        | Non-streaming assembly                                                                                                                                        |
| ------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Chat content, `reasoning`, `reasoning_content`, and `refusal` | String deltas concatenate under their original message field names.                                                                                           |
| Function `tool_calls`                                         | Calls group by stream index and return in index order; each final call carries the concatenated ID, function name and arguments because the stream index serves as the grouping key. |
| Legacy `function_call`                                        | Name and argument fragments concatenate into one message field.                                                                                               |
| Logprobs                                                      | Chat content/refusal arrays and text-completion token/logprob/offset arrays concatenate in stream order.                                                      |

Each tool call needs an ID and function name, and its arguments are returned as engine-generated strings for the client to handle. Support for tools, reasoning and input formats depends on the configured engine and model.

Non-streaming requests support text output and function tools. Requests for `audio`, `modalities` other than `["text"]`, or tools whose type differs from `function` receive `400` before engine work begins.

During response assembly, Narwhal accepts a fixed set of choice and chat-delta fields; any other non-null field, including audio, annotations and custom tool output, fails the response with `502`. Malformed accepted fields also count as failed requests.

Streaming responses retain the engine's delta shape and fields, subject to the token-ID exposure rule below. Both response paths enforce stream termination and serving limits.

Narwhal builds both engine requests from the submitted body, checking the requested model name before setting `model` to the configured served model. Each request supports one sequence, so `n` or `best_of` above 1 receives `400` to keep the prefill and decode sampling widths consistent.

Both routes require a JSON object and check the following field types before admission whenever the supplied value is non-null.

| Field                        | Required type                        |
| ---------------------------- | ------------------------------------ |
| `model`                      | String                               |
| `stream`                     | Boolean                              |
| `n`, `best_of`, `max_tokens` | Integer; boolean values are rejected |
| `prompt`                     | String or array                      |
| `messages`                   | Array of objects                     |

Invalid JSON, body shape or field type receives `400` with an OpenAI error envelope; diagnostics carry the field name and violated rule.

```json
{"error": {"message": "max_tokens must be an integer", "type": "invalid_request_error", "param": "max_tokens", "code": null}}
```

A validation rejection records one invalid terminal outcome before reserving admission or engine capacity. Fields outside the router's validation pass through to the engine unchanged.

Narwhal generates the response `x-request-id` when the request arrives and assigns a separate ID to each engine attempt and phase for KV ownership. The journal retains the client's original ID as `client_rid` so the request can still be traced end to end.

Ingress owns client authentication and strips client credentials before forwarding the request. Set `engine.engine_api_key_env` to attach the deployment engine credential to every serving and control leg. [Configure Narwhal](07-Configuration.md#engine-authentication) defines this boundary.

Configure ingress to strip client-supplied internal credentials and request IDs before setting trusted replacements, following [Operate Narwhal](04-Operate.md#configure-ingress); Narwhal resolves client identity from those trusted replacements.

### Admission

| Condition                                                                                               | Status | Response detail                                                          |
| ------------------------------------------------------------------------------------------------------- | ------ | ------------------------------------------------------------------------ |
| Invalid JSON, body shape or router-interpreted field type                                               | `400`  | `invalid_request_error` envelope with the field in `param`               |
| Requested model differs from `model`                                                                    | `404`  | Error code `model_not_found`                                             |
| `n` or `best_of` exceeds 1                                                                              | `400`  | Invalid sampling width                                                   |
| Non-streaming request asks for audio, non-text output modalities or non-function tools                  | `400`  | `invalid_request_error` naming the rejected option in `param`            |
| HTTP retention limit or admission queue is full                                                          | `429`  | `retry-after: 1`                                                         |
| Request body exceeds `serving.max_request_bytes`                                                        | `413`  | `request_too_large`                                                      |
| Admission wait or original deadline expires before headers                                              | `504`  | Terminal expiry                                                          |
| Predictive admission prices the queue over the TTFT budget                                              | `429`  | `retry-after` contains the rounded queue overrun                         |
| The prompt alone exceeds the TTFT budget                                                                | `429`  | Error envelope alone; shorten the prompt or raise the target.            |
| Every engine is excluded from placement                                                                 | `503`  | Error code `backend_unavailable` with `retry-after: 1`                   |
| Router is a standby, fenced, in whole-wave maintenance, or monitoring-degraded                          | `503`  | Retryable refusal with `retry-after: 1`; inspect `/ready` for the reason |

Global counters separate capacity rejections under `rejected` from predictive refusals under `refused`; malformed bodies increment `invalid_requests`.

Every request arriving at a supported completion endpoint increments `offered` once. A body ending before sizing also increments `unsized_offered` before the router measures its workload shape.

Set `admission` to `open` to disable predictive refusal. Concurrency limits still apply.

### Backend continuation contract

The shipping vLLM/NIXL path requests a non-streaming, one-token producer completion. The producer's generated output is discarded. `PrefillResult` binds the backend-owned KV descriptor to the producer URL, endpoint, and request ID. Decode continues from the original prompt.

Remote decode receives the original prompt or messages, requested output limit, and sampling settings with the validated descriptor attached. Every client-visible output token comes from decode. Same-worker decode removes transfer parameters, including client-supplied ones, and uses engine prefix caching or prompt recomputation.

Descriptor validation rejects missing engine identity, malformed block IDs, and connector or endpoint mismatches before decode HTTP dispatch. It preserves opaque runtime fields. Preflight and occupied-role canaries test transfer correctness for the pinned engine contract.

The original request lifecycle owns the handoff-age limit, phase reservations, retries, and cleanup. The age starts at the beginning of the producer HTTP leg. Each retry uses fresh backend request IDs and producer ownership. Producer HTTP completion, first client-visible decode output, and the intervening handoff delay remain separate measurements.

Python callers use `EngineClient` from `narwhal.engines.client` and `PrefillResult` from `narwhal.engines.connector`. Pass the result returned by `EngineClient.prefill()` directly to `EngineClient.decode()`. `result.parameters()` provides a detached dictionary for inspection. Internal Python paths may change between releases.

### Engine failures

A failed leg returns `504` for timeout-shaped faults and `502` for other engine faults. A prefill failure occurs before streaming starts and can use the HTTP status directly.

An ejected engine in a fleet with an `engine_contract` must pass lifecycle validation before breaker readmission. Uncontracted development fleets use health-only recovery.

Once an engine reaches `recovery.eject_after` consecutive stream failures, including first-token timeouts or mid-stream silence, Narwhal removes it from placement until it passes an inference probe. After a crossed-decode failure, the probe starts from a fresh handoff produced by the original producer. Each probe leg uses `engine.first_token_timeout_s`; inconclusive probes return on the readmission cadence. Whole-wave restart rules continue to govern the fleet.

A successful engine stream emits generated output followed by `data: [DONE]`. Closure before that marker is an engine failure. A marker received before the first generated token produces `502` with `stream ended with [DONE] before any token arrived`. Upstream error objects inside an HTTP 200 stream propagate with their own status.

`engine.first_token_timeout_s` limits the time from opening the decode HTTP stream to receiving the first generated token.

After the first token, `engine.decode_read_timeout_s` bounds silence between transport chunks. Metadata chunks reset that read timeout. A timeout produces a `504` detail beginning `engine went silent between tokens`. A zero value uses the original request deadline as the stream bound.

Each admitted request gets one prefill/decode attempt by default. Transient retries can start a fresh attempt before visible output, within the same request deadline and retry budget. A positive `recovery.failure_quarantine_s` holds the failed engine out of subsequent placement while the breaker catches up. Streaming decode failures emit a terminal `data: {"error": ...}` event under the committed HTTP 200 status.

For engines that support token IDs, Narwhal sets `return_token_ids: true` and `stream_interval: 1` on decode requests and counts identified tokens from text, reasoning and tool-call output.

Exact token IDs are nonnegative integers; booleans are invalid. Serving, profiling and canaries require valid identity for text, reasoning, tool-call and refusal output when counting exact tokens. Invalid identity fails the decode attempt or measurement and marks canary evidence as malformed.

Dialect token accounting uses `token_ids` for exact per-token identity with output length and TPOT scoring, and `unavailable` for every other dialect; decode correction, drift scoring and output-length learning require identified tokens.

Clients receive token IDs when they request `return_token_ids`, in both streaming and non-streaming responses.

Non-streaming responses preserve response metadata and non-null engine `usage` across metadata-only frames. When the engine omits usage, Narwhal supplies it from input length and measured output tokens. The engine supplies `finish_reason` and optional `stop_reason`; both survive a subsequent usage-only frame.

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

Reports process liveness as `ok`, `standby`, `fenced`, `maintenance` or `degraded`, the configured fleet size and the placement-eligible engine count.

```json
{"status": "ok", "instances": 6, "available_instances": 6}
```

Every status returns HTTP 200. `available_instances` counts engines eligible for placement under the router's latest ejection, drain and quarantine evidence; Prometheus scrape targets and breaker state report engine liveness.

### `GET /ready`

Returns HTTP 200 while the router owns control and admits requests. Standby, fencing, lease-storage failure, lifecycle holds, backend loss and monitoring degradation return HTTP 503 with a reason. Load balancers must use this route.

After `controller.monitor_failure_limit` consecutive failed passes, the reason is `monitoring degraded: <stage> <class>`. A standby counts these failures toward takeover. A fully successful monitoring pass clears the degradation.

If every engine is excluded from placement, `/ready` returns HTTP 503 with `reason: no available engines`, while new completion requests receive `backend_unavailable` and `Retry-After: 1`.

A lifecycle or control hold takes precedence. During a whole-wave hold, `/health` reports `maintenance`, `/ready` reports the lifecycle reason, and completion requests receive HTTP 503 with error code `standby`.

The `control_ready` field stays true during backend loss or managed maintenance as long as the router owns its lease and monitoring remains healthy. Standbys use it to keep handoffs current; send client traffic only when `/ready` returns HTTP 200.

### `GET /metrics`

Returns Prometheus text in exposition format version 0.0.4; [Metrics](#metrics) lists the operational series.

### `GET /narwhal/handoff`

Returns a fresh control-plane handoff.

| Field               | Meaning                                                                                                                                                                                     |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `schema`            | `narwhal.handoff`                                                                                                                                                                           |
| `schema_version`    | Handoff schema version. This release writes `1`.                                                                                                                                            |
| `at`                | Unix wall-clock timestamp of the snapshot                                                                                                                                                   |
| `run`               | Request-journal run ID of the writing process                                                                                                                                               |
| `model`             | Configured served model                                                                                                                                                                     |
| `epoch`             | Lease epoch held when the handoff was captured. Zero when HA fencing is disabled.                                                                                                           |
| `holder`            | Unique lease holder token. Empty when HA fencing is disabled.                                                                                                                               |
| `engines`           | Sorted configured engine IDs                                                                                                                                                                |
| `roles`             | Engine ID to current `prefill` or `decode` role                                                                                                                                             |
| `ejected`           | Engine IDs held out by the breaker                                                                                                                                                          |
| `inference_sources` | Suspect engine IDs mapped to the producer IDs required for verification. An empty producer ID requests a local probe.                                                                       |
| `counters`          | `served`, `failed`, `unserved`, `refused`, `rejected`, and `cancelled` totals                                                                                                               |
| `lifecycle`         | Durable engine drain, validation, whole-wave state, restart policy, and accepted process starts                                                                                             |
| `demand_risk`       | Newest consolidation risk event `kind`, elapsed `age_s`, and per-kind counts. The receiver reanchors the age to its clock and collects fresh arrival evidence. `null` marks a clear state. |

Package and Git provenance comes from the journal header. The handoff restores the `served`, `failed`, `unserved`, `refused`, `rejected` and `cancelled` totals. The replacement initializes resident tracking, flip history, role-change and controller-decision counters, latency histograms, floor history and monitoring-failure counters for its process.

The router writes this handoff to `recovery.state_path` for `narwhal-serve --resume`. Warm standbys retrieve it through the HTTP route.

Bind this route to the trusted control network because it exposes resident state, counters and lifecycle details for standby takeover and operator tooling.

### `GET /narwhal/state`

Returns the live scheduler view as `narwhal.state` schema version 1.

| Field                                   | Meaning                                                                                                                                                                                                                                                                                                                                                                                      |
| --------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `schema`                                | `narwhal.state`                                                                                                                                                                                                                                                                                                                                                                              |
| `schema_version`                        | State schema version. This release writes `1`.                                                                                                                                                                                                                                                                                                                                               |
| `served`, `failed`                      | Completed requests and requests ending in error, preserved across resume and takeover                                                                                                                                                                                                                                                                                                        |
| `offered`, `unsized_offered`, `expired` | Completion arrivals, arrivals that terminate before sizing, and deadline expiries in this router process                                                                                                                                                                                                                                                                                     |
| `cancelled`                             | Client disconnects count as their own terminal outcome, preserved across resume and takeover; `served` and `failed` retain separate totals.                                                                                                                                                                                                                                                    |
| `invalid_requests`                      | Client requests rejected as malformed before admission for the current run                                                                                                                                                                                                                                                                                                                   |
| `controller`                            | Active role controller, always `reactive`                                                                                                                                                                                                                                                                                                                                                    |
| `token_accounting`                      | Decode token accounting for this fleet: `token_ids` for exact per-token identity, `unavailable` for every other dialect                                                                                                                                                                                                                                                                       |
| `control`                               | Advisory mode, the latest decision, and process-lifetime decision and role-change totals. `flips` keys are `<caller>:<target-role>`. `flip_reversals`, `flips_refused`, and `flip_inflight` record reversals, refused attempts, and resident requests by phase at applied moves.                                                                                                             |
| `monitoring`                            | Monitoring-loop timing and failure accounting: current and process-high-water event-loop lag, the `degraded` admission gate, the streak's first failure as `reason` (`<class>:<stage>`), the `core_consecutive` failed-pass streak, the lifetime `core_failures` total, and per-stage `failures` totals, `consecutive` streaks, `last_class`, and `last_at`                                                      |
| `ha`                                    | Readiness, standby flag, lease epoch and holder, and any fencing reason                                                                                                                                                                                                                                                                                                                      |
| `lifecycle`                             | Per-engine drain state, resident work, process identities, validation results, and retained events                                                                                                                                                                                                                                                                                           |
| `admission`                             | Active and queued occupancy, admission and phase waiters, limits, rejections, and predictive refusals                                                                                                                                                                                                                                                                                        |
| `serving`                               | Retained HTTP work, attempt counts, retry budget, observed decode tokens, and upstream time                                                                                                                                                                                                                                                                                                  |
| `http_pools`                            | Engine HTTP pool policy: the bounded data pool behind the admission limit, the reserved control pool for health and recovery probes, and the pool-wait timeout                                                                                                                                                                                                                               |
| `pools`                                 | Engine IDs grouped by current prefill or decode role                                                                                                                                                                                                                                                                                                                                         |
| `load`                                  | SLO-relative load per pool. `1.0` is the configured target.                                                                                                                                                                                                                                                                                                                                  |
| `thresholds`                            | Active reactive-controller thresholds                                                                                                                                                                                                                                                                                                                                                        |
| `slo`                                   | TTFT and TPOT targets used by placement and control                                                                                                                                                                                                                                                                                                                                          |
| `first_token_timeout_s`                 | Decode first-token deadline                                                                                                                                                                                                                                                                                                                                                                  |
| `resident`                              | In-flight prefill and decode counts by engine                                                                                                                                                                                                                                                                                                                                                |
| `pinned`                                | Engines excluded from role changes                                                                                                                                                                                                                                                                                                                                                           |
| `min_prefill`                           | Configured live-prefill floor                                                                                                                                                                                                                                                                                                                                                                |
| `min_decode`                            | Configured live-decode floor                                                                                                                                                                                                                                                                                                                                                                 |
| `below_floor`                           | Current and cumulative prefill-floor breach state                                                                                                                                                                                                                                                                                                                                            |
| `ejected`                               | Engines excluded from scheduling by the breaker                                                                                                                                                                                                                                                                                                                                              |
| `draining`                              | Engines excluded by an operator lifecycle action                                                                                                                                                                                                                                                                                                                                             |
| `probation`                             | Engines carrying a predictive-health placement penalty                                                                                                                                                                                                                                                                                                                                       |
| `health`                                | Per-engine drift window counts: `scored` and `undersampled` closed windows with observations, plus `last_scored_s_ago` measuring the silence since a window last reached a verdict. `prefill_paused` marks evidence held for local prefill interference, and `prefill_pauses` counts entries into that state. A confirmed ejection drops the record, so a readmitted engine restarts at zero |
| `quarantined`                           | Engines held out of placement after an engine failure                                                                                                                                                                                                                                                                                                                                        |
| `breaker`                               | Per-engine breaker accounting: consecutive failure streaks keyed by class (`connection`, `timeout`, `overload`, `inference_status`, `kv_handoff`, `stream`, plus `liveness` sweep misses), and the engines with a health or inference verification probe in flight                                                                                                                           |
| `decode_floor`                          | Live decode count, configured minimum, deficit state, and cumulative restorations.                                                                                                                                                                                                                                                                                                           |
| `attainment`                            | Bounded diagnostic SLO outcome buckets                                                                                                                                                                                                                                                                                                                                                       |
| `demand_history`                        | Retained sized and unsized demand, shape counts, and overflow bounds                                                                                                                                                                                                                                                                                                                         |
| `demand_evidence`                       | Consolidation evidence behind every D-to-P gate: retained span and samples with their minimums and the bounded lookback, window closure, short and long decode estimates with their trend ratio, the conservative envelope, the armed risk event and per-kind counts                                                                                                                         |
| `unserved`                              | Phase placements whose eligible candidates all exceeded the configured SLO                                                                                                                                                                                                                                                                                                                   |
| `panic_bypasses`                        | Prefill-to-decode moves allowed through cooldown by the panic condition                                                                                                                                                                                                                                                                                                                      |
| `flips_refused`                         | The 20 most recent role-change refusals                                                                                                                                                                                                                                                                                                                                                      |
| `flips`                                 | Role changes retained up to `flip_history`                                                                                                                                                                                                                                                                                                                                                   |

The nested admission and serving records use these fields.

| Object           | Field                                                              | Meaning                                                                                 |
| ---------------- | ------------------------------------------------------------------ | --------------------------------------------------------------------------------------- |
| `admission`      | `inflight`                                                         | Requests holding a router admission seat                                                |
| `admission`      | `queued`, `queue_capacity`, `queue_high_water`                     | Current admission waiters, configured queue bound, and peak waiters                     |
| `admission`      | `waiting_prefill`, `waiting_decode`                                | Requests waiting for phase dispatch                                                     |
| `admission`      | `limit`                                                            | Effective router limit after `--max-concurrent` precedence                              |
| `admission`      | `rejected`                                                         | Global capacity rejections                                                              |
| `admission`      | `refused`                                                          | Global predictive-admission refusals                                                    |
| `admission`      | `engine_auth`                                                      | Engine-authentication mode: `boundary` or `engine-credential`.                          |
| `serving`        | `http_retained`, `http_retained_limit`, `http_retained_high_water` | Completion requests retaining HTTP resources, their bound, and peak occupancy           |
| `serving`        | `prefill_attempts`, `decode_attempts`, `retry_attempts`            | Cumulative actual phase dispatches and additional prefill attempts                      |
| `serving`        | `retry_credits`, `retry_credits_spent`, `retry_denied`             | Available shared retry credit, spent credit, and denied retries                         |
| `serving`        | `decode_tokens_observed`, `upstream_seconds`                       | Observed decode tokens and cumulative HTTP leg time by phase, including failed attempts |

`pools`, `load`, `resident`, and the control objects have these shapes.

| Object                  | Fields                                                                                                                                                                                                                                      |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `pools`                 | `prefill`, `decode` arrays of engine IDs                                                                                                                                                                                                    |
| `http_pools`            | `data_connections`, `control_connections`, `pool_timeout_s`                                                                                                                                                                                 |
| `load`                  | `prefill`, `decode` SLO-relative floats                                                                                                                                                                                                     |
| `thresholds`            | `expand`, `shrink`, `cooldown_s`, `sustained_intervals`, `dwell_s`, `panic_ratio`                                                                                                                                                           |
| `slo`                   | `ttft_s`, `tpot_s`                                                                                                                                                                                                                          |
| `resident.<iid>`        | `prefill`, `decode` in-flight counts                                                                                                                                                                                                        |
| `below_floor`           | `active`, `live_prefill`, `since`, `breaches`, `cumulative_s`                                                                                                                                                                               |
| `attainment`            | `bucket_s`, `retained_s`, `covered_s`, `buckets`, `outcomes`, `pruned_buckets`, `pruned_outcomes`                                                                                                                                           |
| `demand_evidence`       | `span_s`, `arrivals`, `required_span_s`, `required_arrivals`, `max_span_s`, `closed`, `risk_kind`, `risk_age_s`, `risk_events`, `short_decode_engines`, `long_decode_engines`, `trend_ratio`, `envelope_decode_engines`, `blocked_gate`     |
| `control`               | `advisory`, `last_decision`, cumulative `decisions` by caller and result                                                                                                                                                                    |
| `control.last_decision` | Current and proposed split, demand reference, phase work, projected SLO ratios, applied decode request limit, decode profile coverage and correction, objective change, reason, and result; D-to-P steps also carry the consolidation evidence snapshot |
| `decode_floor`          | `min_decode`, `live_decode`, `below_floor`, `restoration_moves`                                                                                                                                                                             |
| `flips_refused[]`       | `at`, requested `to` role, and `why`                                                                                                                                                                                                        |

Scored reactive decisions include `eligibility_rule`: `source_shrink` for ordinary consolidation, `mixed_pressure` for observed prefill recovery above the decode shrink threshold, or `projected_ttft_recovery` for an arrival-triggered D-to-P evaluation. Eligible proposals also expose `confirmations` and `required_confirmations`.

Projected-TTFT recovery records `trigger_rid`, `projected_ttft_s`, `ttft_slo_s`, `trigger_projected_ttft_ratio`, `resident_prefill_s`, `queued_prefill_s`, `waiting_prefill`, `initial_projected_ttft_s`, `urgent_signals` and `event_to_evaluation_s`. A scored adjacent candidate also records `candidate_projected_ttft_s`, `candidate_projected_ttft_ratio`, `projected_ttft_improvement_s`, `decode_capacity_safe`, `role_floors_safe` and `source_pressure_safe`. `decision_basis` is `projected_ttft_recovery` for this path. The existing `projected_ttft_ratio` remains the demand model's candidate-split ratio.

Held or blocked decisions retain the proposed split and its objective change alongside the responsible constraint, including consolidation evidence, profiles, KV capacity, role floors, pins, cooldown, dwell or the resident guard. Decisions made before scoring, such as insufficient demand history or fleet health changes, carry the fields available at that stage.

`below_floor.live_prefill` counts placement-eligible prefill engines. Role changes observe both configured floors, although health and operator hold-outs can breach them. The controller moves healthy decode capacity into prefill until the floor is restored or `min_decode` prevents another move. Aggregate mode treats its opening zero-prefill pool as the baseline and leaves `below_floor.active` false until the fleet first reaches the floor.

`below_floor.since` uses the process monotonic clock and is `null` outside a breach. `below_floor.cumulative_s` includes the open interval when `active` is true.

The `attainment` object reports SLO outcomes for diagnostics. Completed, failed, expired and predictively refused requests add TTFT-met, TPOT-met and total counts to buckets of width `monitor_interval_s`. Pruning follows the newest recorded bucket, retains four demand windows and includes the complete boundary bucket in window queries.

`attainment` exposes retained and pruned counts. `covered_s` is the age of the oldest retained bucket, capped at the retention span.

A role change has this shape.

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

`by` names the caller as `reactive`, `decode_floor`, or `floor_recovery`. The in-flight fields capture resident work at the label change, and `drained_s` receives its duration when that work finishes.

Narwhal writes per-request evidence to the [request journal](#request-journal).

#### Demand accounting

Within `demand_history.unsized`, `pending` counts bodies being read and `observations` counts retained offers that ended before sizing; the history retains authenticated offers that passed validation.

Parsed offers enter demand history with local input estimates. An admitted request's tokenizer result replaces its estimate at the original arrival time and preserves the single offer. Requests rejected before tokenization retain their local estimates. Repricing preserves bounded shape aggregation and invalidates boundary evidence when its last observation moves to another cohort.

Demand history stores counts in time buckets with at most 128 exact shapes and one overflow cohort per bucket. The unsized-offer history needs only one shape. Bucket width is the smallest of one second, the control step and the minimum evidence span. Arrivals and residency retain one demand window; completed output observations retain four. Recording prunes expired buckets even if the control loop stops.

Excess shapes keep every count and use the largest input and requested output lengths to price work conservatively. Uncapped overflow remains incomplete demand. Output-history overflow disables learned discounts until it expires. A cohort crossing a window boundary contributes its whole count to demand, adding at most one bucket of history. Consolidation evidence counts only observations guaranteed to follow its cutoff. `demand_history` and `narwhal_demand_history_*` expose retained cells, the cell limit, counted observations and overflow observations.

Each reactive decision captures profile coefficients, offered-work demand, observed phase pressure, pending output estimates and old-role resident work before comparing splits. Frozen profiles and value-only split inputs prevent later live-state changes from altering those candidate scores. Output-length estimates and decode correction are built once and shared with both demand horizons.

Priced prefill waiters set `recovery_prefill_ratio` to the larger of observed prefill pressure and resident-plus-queued prefill seconds divided by the current prefill engine count and TTFT SLO. Observed pressure supplies the ratio at zero priced prefill waiters. Incomplete-demand decisions expose this ratio, `queued_prefill_s`, both observed phase ratios and a `decision_basis` of `prefill_pressure_recovery` or `decode_pressure_recovery`. [Role control](07-Configuration.md#role-control) defines the movement and confirmation gates.

Consolidation evidence keeps full-precision demand through the source-pressure and movement checks. Rounded state and journal fields are output only. Role floors, live availability, cooldown, dwell, profile coverage and physical KV limits constrain each move.

### `GET /narwhal/lifecycle`

Returns the `narwhal.lifecycle` version 1 operator document. `router.controls_fleet` distinguishes the active lease holder from a standby or fenced process. Engine records expose `state`, `draining`, scheduler eligibility in `accepts_new`, `ready_to_stop`, resident prefill and decode counts, deadline, wave ID, old and new process starts, validation checks, and an error. The wave record reports whether router-wide readiness is withdrawn and whether every member is safe for the external supervisor to stop. Top-level `engine_restart_policy` names the configured policy. `process_starts` maps engine IDs to the last accepted process-start timestamps. The top-level `error` is empty on success and describes a rejected action on non-2xx responses.

### `POST /narwhal/lifecycle/drain`

Accepts one engine or the complete fleet as a wave. It excludes every target from placement before capturing process identities. A whole wave also withdraws router `/ready`. HTTP 409 rejects an unsafe shape; HTTP 503 reports identity capture failure while preserving the drain hold-out.

```json
{"engines":["e0"],"deadline_s":300}
```

### `POST /narwhal/lifecycle/readmit`

Validates health, process-bound attestation, configured model, direct generation, role-permitted KV transfer, and final health before removing the hold-out. A planned restart also requires a newer process start. Breaker recovery records accept the current process identity because an ejection can result from a transient fault. With `recovery.engine_restart_policy: whole_wave`, every action covers members with a recorded drain identity and newer process. An automatic hold requires an explicit wave drain to capture those identities before restarting. HTTP 409 leaves failed candidates blocked.

```json
{"engines":["e0"]}
```

[Operate Narwhal](04-Operate.md#restart-one-engine) defines the external-supervisor sequence and whole-wave rule.

## Request journal

Narwhal writes timing and placement to JSON Lines. `--journal <path>` selects the file; the default places `journal.jsonl` beside `profiles.path`. The file opens in append mode and uses `run` to separate router processes.

The first row records schema, package version, source identity and token-accounting mode. The `source` digest hashes the installed package's Python files with SHA-256. Identical source produces the same digest in a checkout, deployment and wheel.

```json
{"meta":{"schema":"narwhal.journal","schema_version":1,"package":"narwhal-inference","version":"0.1.0","git":"<commit>","source":"sha256:...","token_accounting":"token_ids"}}
```

Each original completion request produces one terminal row, including invalid bodies, capacity refusals, expiry and cancellation. Retries add attempts to the original request's row.

| Field                                        | Meaning                                                                                                                                                                                                                                                                                                                                                                                          |
| -------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `run`, `rid`, `client_rid`                   | Router run, Narwhal request ID, and optional caller request ID.                                                                                                                                                                                                                                                                                                                                  |
| `arrived`                                    | Arrival on the process monotonic clock. Compare it only within one run.                                                                                                                                                                                                                                                                                                                          |
| `input_len`, `output_len`, `wanted_len`      | Prompt size, returned size, and requested output size. On a cancelled row the output length is the delivered token count when measurable.                                                                                                                                                                                                                                                        |
| `ttft_s`, `tpot_s`, `first_byte_s`           | Router timing for prefill, decode, and first visible output.                                                                                                                                                                                                                                                                                                                                     |
| `prefill_iid`, `decode_iid`                  | Engines used for the two legs.                                                                                                                                                                                                                                                                                                                                                                   |
| `crossed`                                    | Whether decode consumed KV from the recorded prefill engine.                                                                                                                                                                                                                                                                                                                                     |
| `token_accounting`                           | How decode output was counted: `token_ids` for exact per-token identity, `unavailable` for every other dialect.                                                                                                                                                                                                                                                                                  |
| `refused`, `refused_cause`                   | Predictive refusal and its priced cause.                                                                                                                                                                                                                                                                                                                                                         |
| `cancelled`, `cancelled_phase`               | Client disconnect during `admission`, `queue`, `backoff`, `prefill` or `decode`.                                                                                                                                                                                                                                                                                                                 |
| `terminal`                                   | `completed`, `failed`, `refused`, `rejected`, `expired`, `invalid` or `cancelled`.                                                                                                                                                                                                                                                                                                               |
| `input_sized`                                | Whether the body reached local sizing. When false, the body ended before sizing and `input_len: 0` marks that early exit.                                                                                                                                                                                                                                                                         |
| `attempts`, `decode_attempts`                | Prefill and decode dispatch counts for this original request.                                                                                                                                                                                                                                                                                                                                    |
| `attempt_failures`                           | Bounded failed-attempt details retained even when a later attempt succeeds: monotonic time, attempt number, phase, engine IDs, exception type/message/status, transient classification, visible-output state, retry decision and scheduled backoff. At most `serving.max_attempts` entries; each message is limited to 240 characters. A scheduled retry can still be cancelled before dispatch. |
| `queue_wait_s`, `duration_s`                 | Total admission/dispatch wait and original lifetime on the router clock.                                                                                                                                                                                                                                                                                                                         |
| `decode_tpot_s`                              | First-to-last observed output-token interval divided by output tokens minus one; null below two tokens or when exact token accounting is unavailable.                                                                                                                                                                                                                                            |
| `decode_tokens_observed`, `upstream_seconds` | Tokens read across all attempts and summed HTTP leg durations, including failed work, transfer and waiting.                                                                                                                                                                                                                                                                                      |
| `error`                                      | Failure or refusal detail. `null` on success and on cancellation.                                                                                                                                                                                                                                                                                                                                |

Cancelled rows retain measured timing and `error: null`. Journal scoring includes terminal request rows except cancellations and predictive refusals. Invalid requests, capacity rejections, expiries and engine failures count as misses. Completion counters also exclude cancellations.

Requests with a null `output_len` or `ttft_s` count as misses in attainment scoring. Refusals, cancellations and errors retain their outcome categories.

`first_byte_s - ttft_s` contains KV transfer and decode queueing. A crossed request usually pays both; a local request can still wait for decode.

Event rows share the journal. Current events cover role-floor breaches and recovery, blocked decode-floor moves, engine lifecycle actions, and monitoring health. Monitoring events are `monitoring_stage_failure` with `stage`, `class` and the per-stage `consecutive` count, `monitoring_degraded` at the `controller.monitor_failure_limit` crossing, and `monitoring_recovered` when a fully successful pass clears the degraded state. Request analysis selects terminal rows and handles event rows separately.

Timing journals expose engine IDs and engine URLs inside failure strings. Sanitize them before publication.

## Profile store

`narwhal-profile` writes one cost-model row per engine to `profiles.path`, in a document containing `"schema": "narwhal.profiles"`, `schema_version: 1` and a `profiles` list. Readers require this versioned structure and measured decode bounds and error evidence.

The router and preflight use the same validator when loading a profile store. A malformed row stops the operation with an error naming the file, engine and field, for example `profiles.json: profile n4: tpot_slope must be positive`.

The profile store must cover exactly the configured engine set. Startup and preflight reject missing or extra rows and list the affected IDs.

If you profile engines separately with `narwhal-profile --only`, combine their measured rows into a complete store before checking or serving the fleet.

| Field                                          | JSON type         | Rule                                                                                                    |
| ---------------------------------------------- | ----------------- | ------------------------------------------------------------------------------------------------------- |
| `iid`                                          | string            | Nonempty.                                                                                               |
| `ttft_a`, `ttft_b`, `ttft_c`                   | number            | Prefill quadratic coefficients, nonnegative.                                                            |
| `tpot_slope`                                   | number            | Decode interval per resident KV token. Strictly positive: a zero slope prices infinite decode capacity. |
| `tpot_intercept`                               | number            | Zero-contention decode interval, nonnegative.                                                           |
| `kv_capacity_tokens`                           | integer, optional | Physical KV capacity. Positive when present, and at least `decode_max_kv_tokens` when both are present. |
| `tpot_request_slope`                           | number            | Decode interval per active sequence. Nonnegative, default `0`.                                          |
| `decode_min_requests`, `decode_max_requests`   | integer           | Measured concurrency domain. Positive, min at most max.                                                 |
| `decode_min_kv_tokens`, `decode_max_kv_tokens` | integer           | Measured resident-KV domain. Positive, min at most max.                                                 |
| `decode_fit_mape`, `decode_cv_mape`            | number            | Fit and leave-one-out cross-validation error, nonnegative.                                              |

Use the declared JSON types: integer fields reject values such as `true`, `"96"` and `1.5`, while number fields accept integers as well as floats. All numeric values must be finite; the parser rejects `NaN` and `Infinity` before reading the rows.

Each fitted row limits decode concurrency to the smaller of its measured `decode_max_requests` and the number of requests that fit the KV budget at the priced context length. The budget uses `decode_max_kv_tokens`, further limited by physical `kv_capacity_tokens` when available. An unusable measured request bound yields zero decode request capacity.

## Metrics

The six request totals survive resume and standby takeover: `narwhal_served_total`, `narwhal_failed_total`, `narwhal_unserved_total`, `narwhal_refused_total`, `narwhal_rejected_total`, and `narwhal_cancelled_total`. Offered/unsized/expired and attempt counters, retry quota, histograms, controller decision and role-change counters, floor counters, and the invalid-request counter restart with the process. Reconcile request journals by `run`; cumulative outcome counters and process-local offered counters have different restart boundaries.

| Group               | Series                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Router ownership    | `narwhal_router_ready`, `narwhal_router_lease_epoch`                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| Monitoring          | `narwhal_monitoring_degraded`, `narwhal_monitoring_core_consecutive_failures`, `narwhal_monitoring_core_failures_total`, `narwhal_monitoring_stage_failures_total`, `narwhal_monitoring_stage_consecutive_failures`, `narwhal_event_loop_lag_seconds`, `narwhal_event_loop_lag_high_water_seconds`                                                                                                                                                                                                                           |
| Request outcomes    | `narwhal_offered_total`, `narwhal_unsized_offered_total`, `narwhal_expired_total`, `narwhal_served_total`, `narwhal_failed_total`, `narwhal_unserved_total`, `narwhal_refused_total`, `narwhal_rejected_total`, `narwhal_cancelled_total`, `narwhal_invalid_requests_total`                                                                                                                                                                                                                                               |
| Attempts and quota  | `narwhal_prefill_attempts_total`, `narwhal_decode_attempts_total`, `narwhal_retry_attempts_total`, `narwhal_retry_credits`, `narwhal_retry_credits_spent_total`, `narwhal_retry_denied_total`, `narwhal_decode_tokens_observed_total`, `narwhal_upstream_seconds_total`                                                                                                                                                                                                                                                   |
| Queueing            | `narwhal_queued`, `narwhal_queue_capacity`, `narwhal_queue_high_water`, `narwhal_waiting_prefill`, `narwhal_waiting_decode`, `narwhal_queue_wait_seconds`                                                                                                                                                                                                                                                                                                                                                                 |
| HTTP retention      | `narwhal_http_retained`, `narwhal_http_retained_limit`, `narwhal_http_retained_high_water`                                                                                                                                                                                                                                                                                                                                                                                                                                |
| Pools               | `narwhal_pool_instances`, `narwhal_pool_load`, `narwhal_instance_role`, `narwhal_resident_requests`                                                                                                                                                                                                                                                                                                                                                                                                                       |
| Health              | `narwhal_ejected_instances`, `narwhal_ejected`, `narwhal_probation_instances`, `narwhal_health_windows_scored_total`, `narwhal_health_windows_undersampled_total`, `narwhal_health_prefill_paused`, `narwhal_health_prefill_pauses_total`, `narwhal_engine_breaker_streak`, `narwhal_engine_breaker_verifying`                                                                                                                                                                                                            |
| Floors              | `narwhal_prefill_below_floor`, `narwhal_decode_floor`, `narwhal_decode_below_floor`, `narwhal_prefill_below_floor_events_total`, `narwhal_prefill_below_floor_seconds_total`, `narwhal_decode_floor_restorations_total`                                                                                                                                                                                                                                                                                                   |
| Controller          | `narwhal_flips_total`, `narwhal_flip_reversals_total`, `narwhal_flips_refused_total`, `narwhal_flip_inflight_total`, `narwhal_controller_advisory`, `narwhal_controller_decisions_total`, `narwhal_controller_proposed_engines`, `narwhal_controller_phase_work_engines`, `narwhal_controller_projected_slo_ratio`, `narwhal_controller_objective`, `narwhal_controller_decode_tokens_per_engine`, `narwhal_controller_decode_requests_per_engine`, `narwhal_controller_decode_model`, `narwhal_controller_last_decision` |
| Demand history      | `narwhal_demand_history_cells`, `narwhal_demand_history_cell_limit`, `narwhal_demand_history_observations`, `narwhal_demand_history_overflow_observations`                                                                                                                                                                                                                                                                                                                                                               |
| Attainment evidence | `narwhal_attainment_evidence_covered_seconds`, `narwhal_attainment_evidence_outcomes`, `narwhal_attainment_evidence_buckets`, `narwhal_attainment_evidence_pruned_total`                                                                                                                                                                                                                                                                                                                                                  |
| Latency             | `narwhal_slo_seconds`, `narwhal_ttft_seconds`, `narwhal_tpot_seconds`, `narwhal_seat_seconds`                                                                                                                                                                                                                                                                                                                                                                                                                              |
| Lifecycle           | `narwhal_engine_draining`, `narwhal_engine_ready_to_stop`                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |

Role-change totals accumulate from scheduler startup. `narwhal_flip_reversals_total` counts an engine's moves back from its previous recorded target role, starting with its second recorded move. `narwhal_flips_refused_total` counts attempts blocked by timing, availability, pins, floors, the resident guard or advisory mode. Controller decision metrics record evaluation outcomes.

Pool load uses the phase-specific normalization defined in [Role control](07-Configuration.md#role-control). A value of `1.0` reaches the phase target.

`narwhal_slo_seconds` exports the configured `ttft` and `tpot` budgets through its `metric` label. TTFT and TPOT histogram edges use these fractions of the corresponding budget: `0.025`, `0.05`, `0.1`, `0.2`, `0.35`, `0.5`, `0.7`, `1.0`, `1.5`, `3.0`, `10.0`, and `+Inf`. Queue-wait and seat-time histograms use their configured request-lifecycle bounds. Calculate quantiles from bucket rates grouped by `instance` and `le`; combining routers requires identical bucket edges.

The attainment evidence gauges describe the controller's retained outcome buckets. `narwhal_attainment_evidence_pruned_total` appears only after evidence older than every consumer's horizon has dropped, labelled by `kind` (`buckets`, `outcomes`).

### Demand history and decode floor

Each scrape exports the configured decode floor and one demand-history sample per retained window. A router restart starts each history at zero; `narwhal_decode_floor` reads the new process's fleet config.

| Metric | Type | Labels | Value |
| --- | --- | --- | --- |
| `narwhal_decode_floor` | gauge | unlabelled | Configured `min_decode`. |
| `narwhal_demand_history_cells` | gauge | `window`: `unsized`, `arrivals`, `expected_decode`, `observed_decode`, `residency` | Retained demand cohorts. |
| `narwhal_demand_history_cell_limit` | gauge | `window`: `unsized`, `arrivals`, `expected_decode`, `observed_decode`, `residency` | Maximum retained cohorts. |
| `narwhal_demand_history_observations` | gauge | `window`: `unsized`, `arrivals`, `expected_decode`, `observed_decode`, `residency` | Original observations counted in the window. |
| `narwhal_demand_history_overflow_observations` | gauge | `window`: `unsized`, `arrivals`, `expected_decode`, `observed_decode`, `residency` | Observations coalesced after the cohort limit. |

### Consolidation evidence

| Metric                                            | Meaning                                                                                          | Labels                              |
| ------------------------------------------------- | ------------------------------------------------------------------------------------------------ | ----------------------------------- |
| `narwhal_demand_evidence_span_seconds`            | Retained arrival-window duration in seconds                                                      | None                                |
| `narwhal_demand_evidence_arrivals`                | Arrival samples in the retained window                                                           | None                                |
| `narwhal_demand_evidence_closed`                  | 1 when the evidence window has closed                                                            | None                                |
| `narwhal_demand_evidence_risk_age_seconds`        | Age of the newest risk event in seconds                                                          | None                                |
| `narwhal_demand_evidence_short_decode_engines`    | Short-horizon decode demand in engine equivalents                                                | None                                |
| `narwhal_demand_evidence_envelope_decode_engines` | Conservative decode demand envelope in engine equivalents                                        | None                                |
| `narwhal_demand_evidence_trend_ratio`             | Short-horizon demand divided by long-horizon demand; emitted when a long-horizon estimate exists | None                                |
| `narwhal_demand_evidence_refused`                 | 1 for the gate blocking consolidation; all gates are 0 when consolidation is open                | `gate`: `risk`, `evidence`, `trend` |
| `narwhal_demand_evidence_risk_events_total`       | Risk-event count; emitted after the first recorded event                                         | `kind`                              |

### Monitoring

| Metric                                          | Meaning                                                        | Labels  |
| ----------------------------------------------- | -------------------------------------------------------------- | ------- |
| `narwhal_monitoring_degraded`                   | 1 while repeated failed monitoring passes block new admissions | None    |
| `narwhal_monitoring_core_consecutive_failures`  | Consecutive failed monitoring passes                           | None    |
| `narwhal_monitoring_core_failures_total`        | Failed monitoring passes over the process lifetime             | None    |
| `narwhal_monitoring_stage_failures_total`       | Failure count for a monitoring stage                           | `stage` |
| `narwhal_monitoring_stage_consecutive_failures` | Consecutive failures for a monitoring stage                    | `stage` |
| `narwhal_event_loop_lag_seconds`                | Delay beyond the latest scheduled monitoring deadline          | None    |
| `narwhal_event_loop_lag_high_water_seconds`     | Largest monitoring deadline delay in this router process       | None    |

The stages are `controller`, `health`, `drains`, `rollover`, `readmission`, `liveness`, `handoff` and `telemetry`. Telemetry covers floor-state refresh and loop logging.

### Engine breakers

| Metric                             | Meaning                                                                                  | Labels         |
| ---------------------------------- | ---------------------------------------------------------------------------------------- | -------------- |
| `narwhal_engine_breaker_streak`    | Consecutive failures for an engine and failure class; zero-valued series remain exported | `iid`, `class` |
| `narwhal_engine_breaker_verifying` | 1 while an engine's verification probe is running                                        | `iid`, `kind`  |

Failure classes are `connection`, `timeout`, `overload`, `inference_status`, `kv_handoff`, `stream` and `liveness`. Verification kinds are `verify_health` and `verify_inference`.

A resolved probe clears the relevant failure streaks or ejects the engine, reported by `narwhal_ejected`.

`tools/prometheus-alerts.yml` defines the shipped alert expressions. `tools/grafana-narwhal.json` defines the dashboard. [Dashboard definitions](https://github.com/athrael-soju/Narwhal/blob/main/tools/observability/README.md) describe panel scopes and metric boundaries.

## Canary artifacts

`narwhal-canary` loads a `narwhal.canary-cases` version 1 JSON object before dispatching requests. Copy [`config/canary-cases.example.json`](https://github.com/athrael-soju/Narwhal/blob/main/config/canary-cases.example.json) and replace its model-specific values.

| Field | Type | Contract |
| --- | --- | --- |
| `model` | string, optional | Served model used unless `--model` overrides it. |
| `cases` | nonempty array | Exact-output cases with unique IDs. |
| `cases[].id` | string | Stable case identity retained in result rows. |
| `cases[].prompt` | string | Request prompt retained in the input file. |
| `cases[].expected` | string | Exact completion text. |
| `cases[].expected_token_ids` | integer array | Positive token IDs for the exact completion. |
| `cases[].allowed_token_ids` | integer array | Unique strict superset of the expected IDs, including at least one distractor. |

The command writes `narwhal.canary` version 1 JSON Lines in this order:

| Row | Fields |
| --- | --- |
| Build provenance | `meta` containing the schema, package version, Git description and source digest. |
| Run envelope | `meta` containing the schema, `kind: narwhal-canary`, model, rate, duration, timeout, and case filename, SHA-256 and IDs. |
| Request outcome | `kind: canary`; sequence and case IDs, schedule/start/completion times, dispatch delay, TTFT, latency, expected and observed token counts, correctness, status, optional HTTP status and optional completion digest. |
| Control event | `kind: control_event`; event, client-clock timestamp, source, optional engine ID and optional destination role. |
| Summary | `kind: canary_summary`; request and correctness counts, terminal error count, status counts, TTFT percentiles and per-event windows. |

Each request ends with `correct`, `wrong`, `truncated`, `malformed`, `terminal_error`, `timeout`, `http_error` or `transport_error`. Terminal errors increment both `correctness_failures` and `terminal_errors`. `--digest` stores a run-keyed HMAC-SHA-256 of the completion while the result rows retain counts and verdicts.

## Contract versions

The interfaces below carry a schema name and version. Readers apply data from documents declaring the matching schema name and current version; every other document fails validation. Inspect the installed contract set with:

```bash
narwhal-check --print-contract-versions
```

| Interface             | Schema                      | Version |
| --------------------- | --------------------------- | ------: |
| Native fleet config   | `narwhal.fleet`             |       1 |
| Engine profile store  | `narwhal.profiles`          |       1 |
| Engine attestation    | `narwhal.attestation`       |       1 |
| Router handoff        | `narwhal.handoff`           |       1 |
| Router lease          | `narwhal.router-lease`      |       1 |
| Engine lifecycle      | `narwhal.lifecycle`         |       1 |
| Request journal       | `narwhal.journal`           |       1 |
| Live state            | `narwhal.state`             |       1 |
| Prometheus metrics    | `narwhal.metrics`           |       1 |
| Canary cases          | `narwhal.canary-cases`      |       1 |
| Canary results        | `narwhal.canary`            |       1 |
| Contract manifest     | `narwhal.contract-manifest` |       1 |

Documented fields in these versions are compatibility commitments. An incompatible change receives a new schema version. Native Python modules, undocumented fields, log prose and human-readable tables may change between releases.

Compare contract manifests before an upgrade. Keep the previous code, configuration, profiles and compatible state as one rollback set.
