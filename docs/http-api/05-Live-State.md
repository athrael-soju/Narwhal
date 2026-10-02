---
description: Read live admission, scheduler and role controller state from GET /narwhal/state.
---

# Live router and scheduler state

## `GET /narwhal/state`

Returns the live scheduler and router state as `narwhal.state` schema version `1`.

### Top-level fields

The state document carries these top-level fields:

| Field                     | Meaning                                                                               |
| ------------------------- | ------------------------------------------------------------------------------------- |
| `schema`                  | `narwhal.state`                                                                       |
| `schema_version`          | State schema version, `1`                                                             |
| `journal_run`             | Request-journal run ID of the current router process                                  |
| `served`                  | Completed requests                                                                    |
| `slo_met`                 | Completed requests within `slo.ttft_s` and `slo.tpot_s`                               |
| `failed`                  | Requests ending in error                                                              |
| `offered`                 | Completion arrivals                                                                   |
| `unsized_offered`         | Arrivals that terminated before workload sizing                                       |
| `expired`                 | Deadline expiries                                                                     |
| `cancelled`               | Client disconnects                                                                    |
| `invalid_requests`        | Requests refused with HTTP `400`, `404`, or `413` before admission                    |
| `controller`              | Active role controller, `reactive`                                                    |
| `token_accounting`        | `token_ids` for exact token identity, otherwise `unavailable`                         |
| `control`                 | Role-controller mode and its decision counts                                          |
| `monitoring`              | Engine-monitoring loop timing and failure state                                       |
| `ha`                      | High-availability readiness, standby, lease, and failover-block state                 |
| `lifecycle`               | Drain state, resident work, and lifecycle events                                      |
| `admission`               | Router and phase occupancy, queue state, and limits                                   |
| `serving`                 | Retained HTTP work, attempts, and retry state                                         |
| `http_pools`              | Data and control connection pools and pool-wait timeout                               |
| `pools`                   | Engines grouped by prefill or decode role                                             |
| `load`                    | Per-pool load as a ratio to the service-level objective (SLO) target, `1.0` at target |
| `thresholds`              | Active reactive-controller thresholds                                                 |
| `slo`                     | Time to first token (TTFT) and time per output token (TPOT) targets                   |
| `first_token_timeout_s`   | Decode first-token deadline                                                           |
| `first_token_calibration` | First-token calibration verified at router startup, and a label for each engine       |
| `resident`                | In-flight prefill and decode work by engine                                           |
| `pinned`                  | Engines excluded from role changes                                                    |
| `min_prefill`             | Configured minimum live prefill count                                                 |
| `min_decode`              | Configured minimum live decode count                                                  |
| `below_floor`             | Current and cumulative prefill-floor breach state                                     |
| `ejected`                 | Engines removed by the breaker                                                        |
| `peer_release`            | Peer release rounds for each engine out of placement                                  |
| `draining`                | Engines excluded by lifecycle action                                                  |
| `probation`               | Engines carrying a predictive-health placement penalty                                |
| `health`                  | Per-engine drift-window accounting                                                    |
| `quarantined`             | Engines temporarily excluded after engine failure                                     |
| `breaker`                 | Per-engine consecutive failure streaks and probe state                                |
| `residency`               | Per-engine prefix-residency synchronization with its attestation sidecar              |
| `decode_floor`            | Decode floor state and restoration count                                              |
| `attainment`              | SLO outcome buckets for diagnostics                                                   |
| `demand_history`          | Retained demand, shape counts, and overflow state                                     |
| `demand_evidence`         | Consolidation evidence used by decode-to-prefill gates                                |
| `unserved`                | Phase placements where every eligible candidate exceeded the configured SLO           |
| `panic_bypasses`          | Prefill-to-decode moves allowed through cooldown by panic logic                       |
| `flips_refused`           | The 20 most recent rejected role changes                                              |
| `flips`                   | Role changes retained up to `flip_history`                                            |

On resume and takeover, a new router process restores `offered`, `unsized_offered`, `served`, `slo_met`, `failed`, `expired`, `cancelled`, `invalid_requests`, `unserved`, `admission.rejected`, and `admission.refused` from the state handoff. Other counters start at zero.

<div class="grid cards" markdown>

-   [`GET /narwhal/lifecycle`](07-Handoff-and-Lifecycle.md#get-narwhallifecycle)

    ---

    The drain state, resident work, and lifecycle events in `lifecycle`.

-   [SLO attainment and demand accounting](06-SLO-and-Demand.md)

    ---

    The `attainment`, `demand_history`, and `demand_evidence` objects.

-   [Peer memory release](../concepts/03-Failure-and-State.md#peer-memory-release)

    ---

    The release rounds that `peer_release` reports.

-   [Request journal](../telemetry/01-Journal.md#diagnosing-a-request-from-the-journal)

    ---

    Per-request evidence for one client request.

</div>

### `health`

Each engine's `health` entry holds its drift-window accounting:

| Field               | Meaning                                                              |
| ------------------- | -------------------------------------------------------------------- |
| `scored`            | Closed drift windows that produced a score                           |
| `undersampled`      | Closed drift windows whose evidence was too sparse to score          |
| `last_scored_s_ago` | Seconds since the last scored window, `null` until a window scores   |
| `prefill_paused`    | `true` while local prefill interference suspends evidence collection |
| `prefill_pauses`    | Transitions into the paused condition                                |

A confirmed ejection resets the engine's `health` counts to zero.

### `breaker`

`breaker.failures` holds the consecutive failure streaks per engine, keyed by class: `connection`, `timeout`, `overload`, `inference_status`, `kv_handoff`, `stream`, and `liveness` for missed sweeps.

`breaker.verifying` lists the engines with a health or inference probe in flight, each as `iid` and probe `kind`.

### `peer_release`

Under `recovery.engine_restart_policy: individual`, `peer_release` holds one entry per ejected engine and per engine in lifecycle state `drained`, `deadline_exceeded`, `validating`, or `blocked`.

Each entry's `rounds` counts the release rounds sent since the ejection or the last state change. Its `next_round_s` gives the seconds until the next round, or `null` after the last round.

### `residency`

Each engine's `residency` entry tracks its prefix-residency synchronization with its attestation sidecar:

| Field             | Meaning                                                      |
| ----------------- | ------------------------------------------------------------ |
| `known`           | `true` when the router holds the engine's complete residency |
| `reason`          | Cause of the `known` value                                   |
| `epoch`           | Sidecar instance the record follows                          |
| `sequence`        | Last engine event batch applied                              |
| `block_size`      | Tokens per cache block                                       |
| `resident_blocks` | Count of resident blocks per KV cache group                  |
| `resyncs`         | Snapshots taken since router start                           |

`known: false` cases:

| Case                                                          | `reason`                                     |
| ------------------------------------------------------------- | -------------------------------------------- |
| Router awaits its first residency refresh of the engine       | `not yet synchronised`                       |
| Engine has an empty `attestation_url`                         | `engine has no attestation sidecar`          |
| Sidecar answers the residency snapshot route with HTTP 404    | `engine publishes no cache events`           |
| Sidecar answers a residency refresh with an HTTP error status | `residency refresh failed: HTTP <status>`    |
| Residency refresh fails with another error                    | `residency refresh failed: <ExceptionClass>` |
| Sidecar snapshot reports `known: false`                       | Sidecar's reason                             |

For an engine with `known: false`, the router prices each request's prefill on the cold curve of its full input.

The router refreshes each engine's residency view every `controller.monitor_interval_s` seconds.

The router takes a new snapshot when:

- the router starts
- the sidecar reports the requested changes are gone
- the sidecar epoch changes
- the change sequence skips a number
- the previous refresh failed

### `first_token_calibration`

`first_token_calibration` reports the first-token calibration that the router verified at startup:

| Field                  | Meaning                                                               |
| ---------------------- | --------------------------------------------------------------------- |
| `status`               | `measured`, `reused`, or `uncalibrated`                               |
| `captured_at_unix`     | Unix time of the calibration capture, `null` when `uncalibrated`      |
| `candidate_deadline_s` | Calibration candidate deadline in seconds, `null` when `uncalibrated` |
| `engines`              | `measured` or `reused` for each engine, `{}` when `uncalibrated`      |

`status` takes these values:

| `status`       | Condition                                                                                                  |
| -------------- | ---------------------------------------------------------------------------------------------------------- |
| `measured`     | For every engine, the process start that the router last verified matches the calibration                  |
| `reused`       | One or more engines run a relaunched process with the same [process generation](../Core-Concepts.md#terms) |
| `uncalibrated` | `engine.first_token_calibration_path` is empty                                                             |

When the router readmits an engine after a relaunch, `engines` reports `reused` for that engine.

## Admission and serving state

`admission` fields:

| Field              | Meaning                                                               |
| ------------------ | --------------------------------------------------------------------- |
| `inflight`         | Requests holding a router admission seat                              |
| `queued`           | Current admission queue depth                                         |
| `queue_capacity`   | Configured queue bound                                                |
| `queue_high_water` | Peak queue depth                                                      |
| `waiting_prefill`  | Requests waiting for prefill dispatch                                 |
| `waiting_decode`   | Requests waiting for decode dispatch                                  |
| `limit`            | `--max-concurrent` when set, otherwise `serving.max_connections`      |
| `rejected`         | HTTP `429` capacity refusals and HTTP `503` router-readiness refusals |
| `refused`          | Global predictive-admission refusals                                  |
| `engine_auth`      | `boundary` or `engine-credential`                                     |

`serving` fields:

| Field                      | Meaning                                               |
| -------------------------- | ----------------------------------------------------- |
| `http_retained`            | Completion requests retaining HTTP resources          |
| `http_retained_limit`      | Configured retained-request limit                     |
| `http_retained_high_water` | Peak retained-request occupancy                       |
| `prefill_attempts`         | Cumulative prefill dispatches                         |
| `decode_attempts`          | Cumulative decode dispatches                          |
| `retry_attempts`           | Additional prefill attempts                           |
| `retry_credits`            | Available shared retry credit                         |
| `retry_credits_spent`      | Shared retry credit consumed                          |
| `retry_denied`             | Retries refused by budget                             |
| `decode_tokens_observed`   | Observed decode tokens                                |
| `upstream_seconds`         | Cumulative HTTP leg time by phase across all attempts |

## Scheduler and controller state

### Pool and SLO fields

These objects carry the pool, load, SLO, and floor state:

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

The `below_floor` and `decode_floor` fields hold these values:

| Field                      | Meaning                                                                     |
| -------------------------- | --------------------------------------------------------------------------- |
| `below_floor.active`       | `true` during a prefill-floor breach                                        |
| `below_floor.live_prefill` | Prefill engines eligible for placement                                      |
| `below_floor.since`        | Process-monotonic start time of the open breach                             |
| `below_floor.breaches`     | Breaches since tracking started at the first prefill count of `min_prefill` |
| `below_floor.cumulative_s` | Total breach seconds across closed and open breaches                        |
| `decode_floor.live_decode` | Decode engines eligible for placement                                       |

### Controller decisions

`control` fields:

| Field            | Meaning                                                                                      |
| ---------------- | -------------------------------------------------------------------------------------------- |
| `advisory`       | `true` when the role controller records proposed role changes and keeps the live split fixed |
| `last_decision`  | Most recent proposed or applied split, `null` until the first decision                       |
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

| Field                                             | Meaning                                                                                                                                                       |
| ------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `current_prefill`, `current_decode`               | Current prefill and decode split                                                                                                                              |
| `prefill_work`, `decode_work`                     | Estimated prefill and decode demand over the [demand spans](#demand-spans), in engines                                                                        |
| `arrivals`                                        | Arrivals in the prefill demand span                                                                                                                           |
| `output_observations`                             | Completed-output observations in demand history                                                                                                               |
| `demand_complete`                                 | Whether demand history is complete enough to price the decision                                                                                               |
| `projected_ttft_ratio`                            | Demand model's projected TTFT ratio to its target for the candidate split                                                                                     |
| `projected_tpot_ratio`                            | Demand model's projected TPOT ratio to its target for the candidate split                                                                                     |
| `projected_decode_wait_ratio`                     | Candidate split's [decode queueing ratio](../configuration/02-Serving-and-Role-Control.md#75-adjacent-split-decisions)                                         |
| `objective`                                       | Candidate split's objective                                                                                                                                   |
| `objective_delta`                                 | Current objective minus candidate objective, positive for an improvement                                                                                      |
| `decode_request_limit`                            | Applied decode request limit                                                                                                                                  |
| `decode_profile_covered`                          | Whether decode profiles cover the candidate split                                                                                                             |
| `decode_correction`                               | Fleet median of the bounded live-to-profile decode ratio                                                                                                      |
| `decision_basis`                                  | `demand_projection`, `prefill_pressure_recovery`, `decode_pressure_recovery`, or `projected_ttft_recovery`                                                    |
| `observed_prefill_ratio`, `observed_decode_ratio` | Observed phase pressure                                                                                                                                       |
| `recovery_prefill_ratio`, `queued_prefill_s`      | Inputs to the [prefill recovery ratio](06-SLO-and-Demand.md#prefill-recovery-ratio)                                                                           |
| `recovery_decode_ratio`                           | Value of the [decode recovery ratio](06-SLO-and-Demand.md#decode-recovery-ratio)                                                                              |
| `eligibility_rule`                                | Rule applied to a scored proposal                                                                                                                             |
| `demand_horizon_s`                                | Demand span that priced the proposal, in seconds                                                                                                              |
| `steady_horizon_s`                                | Confirmation span, in seconds                                                                                                                                 |
| `steady_prefill_work`, `steady_decode_work`       | Prefill and decode demand over `steady_horizon_s`, in engines. The value is `null` with incomplete demand or on a projected-TTFT recovery evaluation.                 |
| `steady_demand_s`                                 | Seconds since confirmation-span demand began to match window demand. The value is `null` while they differ, with incomplete demand, or on a projected-TTFT recovery evaluation. |
| `departure_age_s`                                 | Seconds since the open departure began                                                                                                                        |
| `confirmations`, `required_confirmations`         | Consecutive confirmations of an eligible proposal and the required count                                                                                      |
| `decode_capacity_safe`                            | Whether the candidate's decode work fits its decode capacity                                                                                                  |
| `role_floors_safe`                                | Whether the candidate respects `min_prefill` and `min_decode`                                                                                                 |
| `source_pressure_safe`                            | Whether source-pool pressure is at or below `shrink`, or whether `mixed_pressure` or `steady_demand` applies                                                 |

Scored decisions add decode capacity fields: `decode_tokens_per_engine`, `decode_slo_capacity_tokens`, `decode_kv_capacity_tokens`, `decode_requests_per_engine`, `pending_decode_requests`, and `pending_decode_tokens`.

Scored decisions add the demand span fields: `demand_horizon_s`, `steady_horizon_s`, `steady_prefill_work`, `steady_decode_work`, and `steady_demand_s`.

Scored decisions during an open departure add `departure_age_s`.

Decode-to-prefill decisions add `risk_kind`, `risk_age_s`, and the [`demand_evidence`](06-SLO-and-Demand.md#consolidation-evidence) fields with an `evidence_` prefix.

`eligibility_rule` takes the first value that applies, in table order:

| `eligibility_rule`        | Proposal                                                                                                              |
| ------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `projected_ttft_recovery` | Urgent decode-to-prefill evaluation triggered by an arriving request                                                  |
| `mixed_pressure`          | Observed prefill recovery exceeds the decode shrink threshold                                                         |
| `settled_departure`       | A [departure's](../concepts/02-Role-Control.md#departures-from-a-settled-split) move, priced on confirmation-span demand |
| `steady_demand`           | [Steady-demand](../concepts/02-Role-Control.md#steady-demand) move with projected source load above `shrink` and at or below `expand` |
| `source_shrink`           | Ordinary consolidation                                                                                                |

#### Demand spans

`demand_horizon_s`, `prefill_work`, `arrivals` and `decode_work` cover these spans:

| Decision                         | `demand_horizon_s`                          | `prefill_work`, `arrivals`                  | `decode_work`                                                                                                                         |
| -------------------------------- | ------------------------------------------- | ------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| Hold before scoring              |                                             | `controller.reactive.window_s`              | `controller.reactive.window_s`                                                                                                        |
| A departure's move               | [Confirmation span](../configuration/02-Serving-and-Role-Control.md#75-adjacent-split-decisions) | Confirmation span                           | Larger of residency over the latest `controller.reactive.step_s` and expected decode over the confirmation span                       |
| Other decode-to-prefill proposal | `controller.reactive.window_s`              | `controller.reactive.window_s`              | Larger of the `controller.reactive.evidence_span_s` and `controller.reactive.window_s` estimates                                      |
| Every other scored decision      | `controller.reactive.window_s`              | `controller.reactive.window_s`              | `controller.reactive.window_s`                                                                                                        |

#### Projected-TTFT recovery fields

A projected-TTFT recovery evaluation adds these fields to `control.last_decision`:

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

| Field                            | Meaning                                                                                           |
| -------------------------------- | ------------------------------------------------------------------------------------------------- |
| `candidate_projected_ttft_s`     | Trigger request's projected TTFT under the candidate split with the least favourable donor engine |
| `candidate_projected_ttft_ratio` | `candidate_projected_ttft_s` divided by the TTFT target                                           |
| `projected_ttft_improvement_s`   | `projected_ttft_s` minus `candidate_projected_ttft_s`                                             |

Scored projected-TTFT recovery decisions set `decision_basis=projected_ttft_recovery`.

Blocked and held decisions keep the proposed split and objective change.

`reason` names the constraint that stopped the move:

- consolidation evidence
- profile coverage
- KV capacity
- role floors
- a role with zero live engines while fleet health is changing
- pins
- cooldown
- dwell
- the resident guard
- a departure's hold on the reverse move

A departure's hold sets one of these `reason` values:

| `reason`                                          | Held move                                               |
| ------------------------------------------------- | ------------------------------------------------------- |
| `departure toward decode holds the reverse move`  | Decode-to-prefill move after a departure toward decode  |
| `departure toward prefill holds the reverse move` | Prefill-to-decode move after a departure toward prefill |

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

| Field              | Meaning                                                                   |
| ------------------ | ------------------------------------------------------------------------- |
| `at`               | Role-change time on the router's monotonic clock                          |
| `iid`              | Engine ID                                                                 |
| `to`               | New role: `prefill` or `decode`                                           |
| `by`               | Caller that changed the role                                              |
| `prefill_inflight` | Resident prefill work when the role label changes                         |
| `decode_inflight`  | Resident decode work when the role label changes                          |
| `drained_s`        | Drain duration in seconds after resident work finishes, `null` until then |

`by` values:

| `by`             | Role change                                                                           |
| ---------------- | ------------------------------------------------------------------------------------- |
| `reactive`       | Role-controller move                                                                  |
| `decode_floor`   | Decode-floor restoration toward `min_decode`                                          |
| `floor_recovery` | Prefill-floor recovery, moving healthy decode engines to prefill down to `min_decode` |

Each `flips_refused[]` record contains `at`, `to`, and `why`.
