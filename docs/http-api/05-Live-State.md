# Live router and scheduler state

## `GET /narwhal/state`

Returns the live scheduler and router state as `narwhal.state` schema version `1`.

### Top-level fields

| Counters                                                           | New router process                                     |
| ------------------------------------------------------------------ | ------------------------------------------------------ |
| `served`, `failed`, `cancelled`, `unserved`, `rejected`, `refused` | Restored from the state handoff on resume and takeover |
| Fields marked "current process" below                              | Start at zero                                          |

| Field                   | Meaning                                                                                         |
| ----------------------- | ----------------------------------------------------------------------------------------------- |
| `schema`                | `narwhal.state`                                                                                 |
| `schema_version`        | State schema version, `1`                                                             |
| `journal_run`           | Request-journal run ID of the current router process, carried by every journal row              |
| `served`                | Completed requests                                                                              |
| `failed`                | Requests ending in error                                                                        |
| `offered`               | Completion arrivals in the current process                                                            |
| `unsized_offered`       | Arrivals in the current process that terminated before workload sizing                                |
| `expired`               | Deadline expiries in the current process                                                              |
| `cancelled`             | Client disconnects                                                                              |
| `invalid_requests`      | Malformed requests the current process rejected before admission                                   |
| `controller`            | Active role controller, `reactive`                                                             |
| `token_accounting`      | `token_ids` for exact token identity, otherwise `unavailable`                                   |
| `control`               | Role-controller mode and its decision counts                                                    |
| `monitoring`            | Engine-monitoring loop timing and failure state                                                 |
| `ha`                    | High-availability state, including readiness and lease                                          |
| `lifecycle`             | Drain state, resident work, and lifecycle events                                                |
| `admission`             | Router and phase occupancy, queue state, and limits                                             |
| `serving`               | Retained HTTP work, attempts, and retry state                                                   |
| `http_pools`            | Data and control connection pools and pool-wait timeout                                         |
| `pools`                 | Engines grouped by prefill or decode role                                                       |
| `load`                  | Per-pool load as a ratio to the service-level objective (SLO) target, `1.0` at target |
| `thresholds`            | Active reactive-controller thresholds                                                           |
| `slo`                   | Time to first token (TTFT) and time per output token (TPOT) targets                             |
| `first_token_timeout_s` | Decode first-token deadline                                                                     |
| `resident`              | In-flight prefill and decode work by engine                                                     |
| `pinned`                | Engines excluded from role changes                                                              |
| `min_prefill`           | Configured minimum live prefill count                                                           |
| `min_decode`            | Configured minimum live decode count                                                            |
| `below_floor`           | Current and cumulative prefill-floor breach state                                               |
| `ejected`               | Engines removed by the breaker                                                                  |
| `draining`              | Engines excluded by lifecycle action                                                            |
| `probation`             | Engines carrying a predictive-health placement penalty                                          |
| `health`                | Per-engine drift-window accounting                                                              |
| `quarantined`           | Engines temporarily excluded after engine failure                                               |
| `breaker`               | Per-engine consecutive failure streaks and probe state                                          |
| `residency`             | Per-engine prefix-residency synchronization with its attestation sidecar                        |
| `decode_floor`          | Decode floor state and restoration count                                                        |
| `attainment`            | SLO outcome buckets for diagnostics                                                             |
| `demand_history`        | Retained demand, shape counts, and overflow state                                               |
| `demand_evidence`       | Consolidation evidence used by decode-to-prefill gates                                          |
| `unserved`              | Phase placements where every eligible candidate exceeded the configured SLO                     |
| `panic_bypasses`        | Prefill-to-decode moves allowed through cooldown by panic logic in the current process                |
| `flips_refused`         | The 20 most recent rejected role changes                                                        |
| `flips`                 | Role changes retained up to `flip_history`                                                      |

| Topic                                             | Reference                                                                                |
| ------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| `lifecycle`                                       | [`GET /narwhal/lifecycle`](07-Handoff-and-Lifecycle.md#get-narwhallifecycle)             |
| `attainment`, `demand_history`, `demand_evidence` | [SLO attainment and demand accounting](06-SLO-and-Demand.md)                             |
| Per-request evidence                              | [Request journal](../telemetry/01-Journal.md#diagnose-a-request-from-the-journal)       |

### `health`

| Field               | Meaning                                                              |
| ------------------- | -------------------------------------------------------------------- |
| `scored`            | Closed drift windows that produced a score                           |
| `undersampled`      | Closed drift windows whose evidence was too sparse to score          |
| `last_scored_s_ago` | Seconds since the last scored window, `null` until a window scores   |
| `prefill_paused`    | `true` while local prefill interference suspends evidence collection |
| `prefill_pauses`    | Transitions into the paused condition                                |
| Confirmed ejection  | Clears the engine's drift-window record                              |

### `breaker`

| Field       | Meaning                                                                                                                                                 |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `failures`  | Consecutive failure streaks per engine, keyed by class: `connection`, `timeout`, `overload`, `inference_status`, `kv_handoff`, `stream`, and `liveness` for missed sweeps |
| `verifying` | Engines with a health or inference probe in flight, each as `iid` and probe `kind`                                                                      |

### `residency`

| Field             | Meaning                                                      |
| ----------------- | ------------------------------------------------------------ |
| `known`           | `true` when the router holds the engine's complete residency, `false` when the sidecar is absent or answers the residency routes with HTTP 404 |
| `reason`          | Cause of the `known` value                                   |
| `epoch`           | Sidecar instance the record follows                          |
| `sequence`        | Last engine event batch applied                              |
| `block_size`      | Tokens per cache block                                       |
| `resident_blocks` | Count of resident blocks per KV cache group                  |
| `resyncs`         | Snapshots taken since router start                           |

`resyncs` increments on:

- the sidecar reports the requested changes are gone
- the sidecar epoch changes
- the change sequence skips a number
- a refresh fails

## Admission and serving state

`admission` fields:

| Field              | Meaning                                                    |
| ------------------ | ---------------------------------------------------------- |
| `inflight`         | Requests holding a router admission seat                   |
| `queued`           | Current admission queue depth                              |
| `queue_capacity`   | Configured queue bound                                     |
| `queue_high_water` | Peak queue depth                                           |
| `waiting_prefill`  | Requests waiting for prefill dispatch                      |
| `waiting_decode`   | Requests waiting for decode dispatch                       |
| `limit`            | Effective router limit, `--max-concurrent` taking precedence |
| `rejected`         | Global capacity rejections                                 |
| `refused`          | Global predictive-admission refusals                       |
| `engine_auth`      | `boundary` or `engine-credential`                          |

`serving` fields:

| Field                      | Meaning                                                      |
| -------------------------- | ------------------------------------------------------------ |
| `http_retained`            | Completion requests retaining HTTP resources                 |
| `http_retained_limit`      | Configured retained-request limit                            |
| `http_retained_high_water` | Peak retained-request occupancy                              |
| `prefill_attempts`         | Cumulative prefill dispatches                                |
| `decode_attempts`          | Cumulative decode dispatches                                 |
| `retry_attempts`           | Additional prefill attempts                                  |
| `retry_credits`            | Available shared retry credit                                |
| `retry_credits_spent`      | Shared retry credit consumed                                 |
| `retry_denied`             | Retries refused by budget                                    |
| `decode_tokens_observed`   | Observed decode tokens                                       |
| `upstream_seconds`         | Cumulative HTTP leg time by phase, including failed attempts |

## Scheduler and controller state

### Pool and SLO fields

| Object           | Fields                                                                            |
| ---------------- | --------------------------------------------------------------------------------- |
| `pools`          | `prefill`, `decode` engine-ID arrays                                              |
| `http_pools`     | `data_connections`, `control_connections`, `pool_timeout_s`                       |
| `load`           | `prefill`, `decode` SLO-relative floats                                           |
| `thresholds`     | `expand`, `shrink`, `cooldown_s`, `sustained_intervals`, `dwell_s`, `panic_ratio` |
| `slo`            | `ttft_s`, `tpot_s`                                                                |
| `resident.<iid>` | `prefill`, `decode` in-flight counts                                              |
| `below_floor`    | `active`, `live_prefill`, `since`, `breaches`, `cumulative_s`                     |
| `decode_floor`   | `min_decode`, `live_decode`, `below_floor`, `restoration_moves`                   |

| Field                      | Meaning                                                                    |
| -------------------------- | -------------------------------------------------------------------------- |
| `below_floor.active`       | `true` during a prefill-floor breach         |
| `below_floor.live_prefill` | Prefill engines eligible for placement                                     |
| `below_floor.since`        | Process-monotonic start time of the open breach |
| `below_floor.breaches`     | Breaches since tracking started at the first prefill count of `min_prefill` |
| `below_floor.cumulative_s` | Total breach seconds, including the open breach                            |
| `decode_floor.live_decode` | Decode engines eligible for placement                                      |
| Prefill-floor recovery     | Moves healthy decode engines to prefill down to `min_decode`               |

### Controller decisions

`control` fields:

| Field            | Meaning                                                                                      |
| ---------------- | -------------------------------------------------------------------------------------------- |
| `advisory`       | `true` when the role controller records proposed role changes and keeps the live split fixed |
| `last_decision`  | Most recent proposed or applied split, `null` until the first decision                      |
| `decisions`      | Cumulative decision counts keyed `<by>:<result>`                                             |
| `flips`          | Cumulative role-change counts keyed `<by>:<to>`                                              |
| `flip_reversals` | Role changes that reverse the same engine's previous role change                             |
| `flips_refused`  | Cumulative count of refused role changes                                                     |
| `flip_inflight`  | Cumulative resident requests carried through role changes, by phase                          |

`control.last_decision` has:

| Field     | Meaning                                                    |
| --------- | ---------------------------------------------------------- |
| `at`      | Decision time on the router's monotonic clock              |
| `prefill` | Proposed prefill engine count                              |
| `decode`  | Proposed decode engine count                               |
| `by`      | Caller that recorded the decision                          |
| `reason`  | Why the role controller applied, held, or blocked the move |
| `result`  | `applied`, `held`, `blocked`, or `advisory`                |
| `applied` | `true` when `result` is `applied`                          |

Optional fields, by evaluation stage:

| Field                                             | Meaning                                                                                                    |
| ------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| `current_prefill`, `current_decode`               | Current split                                                                                              |
| `prefill_work`, `decode_work`                     | Estimated prefill and decode demand, in engines                                                            |
| `arrivals`                                        | Arrivals in the demand window                                                                              |
| `output_observations`                             | Completed-output observations in demand history                                                            |
| `demand_complete`                                 | Whether demand history is complete enough to price the decision                                            |
| `projected_ttft_ratio`                            | Demand model's projected TTFT ratio to its target for the candidate split                                  |
| `projected_tpot_ratio`                            | Demand model's projected TPOT ratio to its target for the candidate split                                  |
| `objective`                                       | Candidate split's objective                                                                                |
| `objective_delta`                                 | Current objective minus candidate objective, positive for an improvement                            |
| `decode_request_limit`                            | Applied decode request limit                                                                               |
| `decode_profile_covered`                          | Whether decode profiles cover the candidate split                                                          |
| `decode_correction`                               | Fleet median of the bounded live-to-profile decode ratio                                                   |
| `decision_basis`                                  | `demand_projection`, `prefill_pressure_recovery`, `decode_pressure_recovery`, or `projected_ttft_recovery` |
| `observed_prefill_ratio`, `observed_decode_ratio` | Observed phase pressure                                                                                    |
| `recovery_prefill_ratio`, `queued_prefill_s`      | Inputs to the [prefill recovery ratio](06-SLO-and-Demand.md#prefill-recovery-ratio)                        |
| `eligibility_rule`                                | Rule that made a scored proposal eligible                                                                  |
| `confirmations`, `required_confirmations`         | Consecutive confirmations of an eligible proposal, against the required count           |
| `decode_capacity_safe`                            | Whether the candidate's decode work fits its decode capacity                                               |
| `role_floors_safe`                                | Whether the candidate respects `min_prefill` and `min_decode`                                              |
| `source_pressure_safe`                            | Whether source-pool pressure is at or below the shrink threshold or `mixed_pressure` applies |

Scored decisions add decode capacity fields: `decode_tokens_per_engine`, `decode_slo_capacity_tokens`, `decode_kv_capacity_tokens`, `decode_requests_per_engine`, `pending_decode_requests`, and `pending_decode_tokens`.

Decode-to-prefill decisions add `risk_kind`, `risk_age_s`, and the [`demand_evidence`](06-SLO-and-Demand.md#consolidation-evidence) fields with an `evidence_` prefix.

`eligibility_rule` values:

- `source_shrink`: ordinary consolidation
- `mixed_pressure`: observed prefill recovery exceeds the decode shrink threshold
- `projected_ttft_recovery`: urgent decode-to-prefill evaluation triggered by an arriving request

#### Projected-TTFT recovery fields

| Field                          | Meaning                                                                       |
| ------------------------------ | ----------------------------------------------------------------------------- |
| `trigger_rid`                  | Router request ID of the queued prefill request that triggered the evaluation |
| `projected_ttft_s`             | Projected TTFT for that request                                               |
| `ttft_slo_s`                   | Configured TTFT target                                                        |
| `trigger_projected_ttft_ratio` | `projected_ttft_s` divided by `ttft_slo_s`                                    |
| `resident_prefill_s`           | Profiled prefill seconds resident on live prefill engines                     |
| `queued_prefill_s`             | Profiled prefill seconds waiting for prefill dispatch                         |
| `waiting_prefill`              | Requests waiting for prefill dispatch                                         |
| `initial_projected_ttft_s`     | Projected TTFT when the first urgent signal arrived                           |
| `urgent_signals`               | Urgent signals coalesced into this evaluation                                 |
| `event_to_evaluation_s`        | Seconds from the urgent signal to this evaluation                             |

A scored candidate moving one engine from decode to prefill adds:

| Field                            | Meaning                                                                                                |
| -------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `candidate_projected_ttft_s`     | Trigger request's projected TTFT under the candidate split, assuming the least favourable donor engine |
| `candidate_projected_ttft_ratio` | `candidate_projected_ttft_s` divided by the TTFT target                                                |
| `projected_ttft_improvement_s`   | `projected_ttft_s` minus `candidate_projected_ttft_s`                                                  |

Scored projected-TTFT recovery decisions set `decision_basis=projected_ttft_recovery`.

Blocked and held decisions keep the proposed split and objective change.

`reason` names the constraint that stopped the move:

- consolidation evidence
- profile coverage
- KV capacity
- role floors
- live availability while fleet health is changing
- pins
- cooldown
- dwell
- the resident guard

With incomplete demand history or a fleet health change before scoring, `control.last_decision` holds the inputs available at that stage.

### Role-change history

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

| Field              | Meaning                                                                       |
| ------------------ | ----------------------------------------------------------------------------- |
| `at`               | Role-change time on the router's monotonic clock                              |
| `iid`              | Engine ID                                                                     |
| `to`               | New role: `prefill` or `decode`                                               |
| `by`               | Caller: `reactive`, `decode_floor`, or `floor_recovery`                       |
| `prefill_inflight` | Resident prefill work when the role label changes                             |
| `decode_inflight`  | Resident decode work when the role label changes                              |
| `drained_s`        | Drain duration in seconds after resident work finishes, `null` until then |

Each `flips_refused[]` record contains `at`, `to`, and `why`.
