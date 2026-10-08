---
description: Read live admission, scheduler and role controller state from GET /narwhal/state.
---

# Live router and scheduler state

## `GET /narwhal/state`

Returns the live router and scheduler state as a `narwhal.state` schema version `1` document.

### Top-level fields

The state document carries these top-level fields:

| Field                     | Meaning                                                                                   |
| ------------------------- | ----------------------------------------------------------------------------------------- |
| `schema`                  | `narwhal.state`                                                                           |
| `schema_version`          | State schema version, `1`                                                                 |
| `journal_run`             | Run ID of the current router process in the request journal                               |
| `served`                  | Completed requests                                                                        |
| `slo_met`                 | Completed requests that met both `slo.ttft_s` and `slo.tpot_s`                            |
| `failed`                  | Requests that ended in an error                                                           |
| `offered`                 | Completion requests received                                                              |
| `unsized_offered`         | Requests that ended before workload sizing                                                |
| `expired`                 | Requests whose deadline expired                                                           |
| `cancelled`               | Requests whose client disconnected                                                        |
| `invalid_requests`        | Requests refused with HTTP `400`, `404`, or `413` before admission                        |
| `controller`              | Active role controller, `reactive`                                                        |
| `token_accounting`        | `token_ids` when the engines supply token IDs, otherwise `unavailable`                    |
| `control`                 | Role-controller mode and decision counts                                                  |
| `monitoring`              | Timing and failure state of the engine monitoring loop                                    |
| `ha`                      | High-availability state: readiness, standby status, lease, and failover block             |
| `lifecycle`               | Drain state, resident work, and lifecycle events                                          |
| `admission`               | Router and phase occupancy, queue state, limits, and admission mode                       |
| `outcome_reasons`         | `failed`, `refused`, `rejected`, and `expired` counts by [reason](../telemetry/01-Journal.md#outcome-reasons) |
| `seats`                   | Each engine's prefill and decode seats and the inputs that set them                      |
| `handoff`                 | Each engine's attested KV lease and the handoff bound derived from it                    |
| `serving`                 | Retained HTTP work, attempts, and retry state                                             |
| `http_pools`              | Data and control connection pools, and the pool-wait timeout                              |
| `pools`                   | Engines grouped by prefill or decode role                                                 |
| `load`                    | Load of each pool as a ratio to the service-level objective (SLO) target, `1.0` at target |
| `thresholds`              | Thresholds the reactive controller applies                                                |
| `slo`                     | Time to first token (TTFT) and time per output token (TPOT) targets                       |
| `first_token_timeout_s`   | Deadline to the first decode token                                                        |
| `first_token_calibration` | First-token calibration verified at router startup, with a label for each engine          |
| `resident`                | In-flight prefill and decode work on each engine                                          |
| `pinned`                  | Engines excluded from role changes                                                        |
| `min_prefill`             | Configured minimum number of live prefill engines                                         |
| `min_decode`              | Configured minimum number of live decode engines                                          |
| `below_floor`             | Current and cumulative breaches of the prefill floor                                      |
| `ejected`                 | Engines the breaker ejected                                                               |
| `peer_release`            | Peer release rounds for each engine out of placement                                      |
| `draining`                | Engines excluded by a lifecycle action                                                    |
| `probation`               | Engines on probation, which carry a placement penalty                                     |
| `health`                  | Drift-window accounting for each engine                                                   |
| `quarantined`             | Engines held out of placement for a time after a failure                                  |
| `breaker`                 | Consecutive failure streaks and probe state for each engine                               |
| `residency`               | Each engine's prefix-residency synchronization with its attestation sidecar               |
| `decode_floor`            | Decode floor state and restoration count                                                  |
| `attainment`              | SLO outcome buckets for diagnostics                                                       |
| `demand_history`          | Retained demand, shape counts, and overflow state                                         |
| `demand_evidence`         | Consolidation evidence used by decode-to-prefill gates                                    |
| `unserved`                | Phase placements where every eligible candidate exceeded the configured SLO               |
| `panic_bypasses`          | Prefill-to-decode moves that panic logic allowed during cooldown                          |
| `flips_refused`           | The 20 most recent refused role changes                                                   |
| `flips`                   | The most recent role changes, up to `flip_history`                                        |

After a resume or takeover, the new router process restores `offered`, `unsized_offered`, `served`, `slo_met`, `failed`, `expired`, `cancelled`, `invalid_requests`, `unserved`, `admission.rejected`, `admission.refused`, and `outcome_reasons` from the state handoff. Its other counters start at zero.

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

### `monitoring`

The `monitoring` object reports the engine monitoring loop:

| Field                         | Meaning                                                                                                                              |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `degraded`                    | `true` while the router holds new admissions after `controller.monitor_failure_limit` consecutive failed passes                      |
| `reason`                      | First failure of the streak that set `degraded`, as `<class>:<stage>`, otherwise `null`                                              |
| `core_consecutive`            | Consecutive monitoring passes with a failed stage                                                                                    |
| `core_failures`               | Monitoring passes with a failed stage since process start                                                                            |
| `event_loop_lag_s`            | Delay beyond the latest scheduled monitoring deadline, in seconds                                                                    |
| `event_loop_lag_high_water_s` | Largest monitoring-deadline delay since process start, in seconds                                                                    |
| `event_loop_busy_s`           | Event-loop thread CPU seconds since the monitoring loop started                                                                      |
| `stages`                      | Each monitoring stage's `failures`, `consecutive`, `last_class` and `last_at` (router monotonic-clock seconds of the latest failure) |

### `health`

Each engine's `health` entry tracks its drift windows:

| Field               | Meaning                                                            |
| ------------------- | ------------------------------------------------------------------ |
| `scored`            | Closed drift windows that produced a score                         |
| `undersampled`      | Closed drift windows whose evidence was too sparse to score        |
| `last_scored_s_ago` | Seconds since the last scored window, `null` until a window scores |
| `prefill_paused`    | `true` while prefill on this engine pauses evidence collection     |
| `prefill_pauses`    | Times `prefill_paused` changed to `true`                           |

A confirmed ejection resets the engine's `health` counts to zero.

### `breaker`

`breaker.failures` holds each engine's consecutive failure streaks, keyed by failure class: `connection`, `timeout`, `overload`, `inference_status`, `kv_handoff`, `stream`, and `liveness` for missed health sweeps.

`breaker.verifying` lists each engine with a health or inference probe in flight, as its `iid` and the probe `kind`.

### `peer_release`

With `recovery.engine_restart_policy: individual`, `peer_release` has one entry for each ejected engine and for each engine in lifecycle state `drained`, `deadline_exceeded`, `validating`, or `blocked`:

| Field          | Meaning                                                         |
| -------------- | --------------------------------------------------------------- |
| `rounds`       | Release rounds sent since the ejection or the last state change |
| `next_round_s` | Seconds until the next round, `null` after the last round       |

### `residency`

Each engine's `residency` entry reports the router's view of the engine's resident prefix blocks, synchronized from its attestation sidecar:

| Field             | Meaning                                                                      |
| ----------------- | ---------------------------------------------------------------------------- |
| `known`           | `true` when the router holds a complete view of the engine's resident blocks |
| `reason`          | Reason for the `known` value                                                 |
| `epoch`           | Sidecar instance that the view follows                                       |
| `sequence`        | Last engine event batch the router applied                                   |
| `block_size`      | Tokens per cache block                                                       |
| `resident_blocks` | Resident blocks in each KV cache group                                       |
| `resyncs`         | Snapshots the router has taken since it started                              |

The router reports `known: false` in these cases:

| Case                                                              | `reason`                                     |
| ----------------------------------------------------------------- | -------------------------------------------- |
| The router has yet to refresh the engine's residency              | `not yet synchronised`                       |
| The engine has an empty `attestation_url`                         | `engine has no attestation sidecar`          |
| The sidecar answers the residency snapshot route with HTTP 404    | `engine publishes no cache events`           |
| The sidecar answers a residency refresh with an HTTP error status | `residency refresh failed: HTTP <status>`    |
| A residency refresh fails with another error                      | `residency refresh failed: <ExceptionClass>` |
| The sidecar snapshot reports `known: false`                       | The sidecar's reason                         |

For an engine with `known: false`, the router prices each request's prefill on the cold curve of its full input.

The router refreshes each engine's residency view every `controller.monitor_interval_s` seconds.

The router takes a new snapshot when:

- the router starts
- the sidecar reports that the requested changes are gone
- the sidecar epoch changes
- the change sequence skips a number
- the previous refresh failed

### `first_token_calibration`

`first_token_calibration` describes the calibration that the router verified at startup:

| Field                  | Meaning                                                                     |
| ---------------------- | --------------------------------------------------------------------------- |
| `status`               | `measured`, `reused`, or `uncalibrated`                                     |
| `captured_at_unix`     | Unix time of the calibration capture, `null` when `uncalibrated`            |
| `candidate_deadline_s` | The calibration's candidate deadline in seconds, `null` when `uncalibrated` |
| `engines`              | `measured` or `reused` for each engine, `{}` when `uncalibrated`            |

`status` takes these values:

| `status`       | Condition                                                                                                  |
| -------------- | ---------------------------------------------------------------------------------------------------------- |
| `measured`     | Every engine runs the process that the calibration timed                                                   |
| `reused`       | One or more engines run a relaunched process with the same [process generation](../Core-Concepts.md#terms) |
| `uncalibrated` | `engine.first_token_calibration_path` is empty                                                             |

When the router readmits an engine after a relaunch, `engines` reports `reused` for that engine.

## Admission and serving state

`admission` fields:

| Field              | Meaning                                                               |
| ------------------ | --------------------------------------------------------------------- |
| `inflight`         | Requests holding a router admission seat                              |
| `queued`           | Current admission queue depth                                         |
| `queue_capacity`   | Configured maximum queue depth                                        |
| `queue_high_water` | Peak queue depth                                                      |
| `waiting_prefill`  | Requests waiting for prefill dispatch                                 |
| `waiting_decode`   | Requests waiting for decode dispatch                                  |
| `limit`            | [In-flight limit](../configuration/02-Serving-and-Role-Control.md#in-flight-limit): `--max-concurrent` when set, otherwise `serving.max_connections` |
| `loop_lag_s`       | Latest router event-loop wake-up lateness                              |
| `sizing_delay_s`   | Median request sizing delay over the last 2 seconds, `0` below 8 sized requests |
| `saturation_threshold_s` | Value of `loop_lag_s` or `sizing_delay_s` at which the router answers a saturation 429, a quarter of `slo.ttft_s` |
| `rejected`         | HTTP `429` in-flight-limit and saturation rejections, and HTTP `503` router-readiness rejections |
| `refused`          | Requests refused by global predictive admission                       |
| `engine_auth`      | `boundary` or `engine-credential`                                     |
| `mode`             | `serving.admission`: `predictive` or `open`                           |
| `margin`           | `serving.admission_margin`                                            |

`seats` fields:

| Field                       | Meaning                                                                                                 |
| --------------------------- | ------------------------------------------------------------------------------------------------------- |
| `mean_input_len`            | Mean sized input length that sets prefill seats, `null` before a request is sized in the window          |
| `engines.<iid>.prefill`     | [Prefill seats](../configuration/02-Serving-and-Role-Control.md#engine-seats), `0` for no limit           |
| `engines.<iid>.decode`      | Decode seats, `0` for no limit                                                                          |
| `engines.<iid>.sequence_limit` | `--max-num-seqs` from the engine's verified attestation, `null` when the launch arguments omit it    |

`handoff` fields, one entry per engine:

| Field                  | Meaning                                                                                              |
| ---------------------- | ---------------------------------------------------------------------------------------------------- |
| `<iid>.kv_lease_s`     | `kv_lease_duration` from the engine's verified attestation, `null` when the attestation records none |
| `<iid>.renewal_s`      | The connector's lease-renewal interval, `kv_lease_s // 6`                                            |
| `<iid>.bound_s`        | [KV handoff bound](../configuration/02-Serving-and-Role-Control.md#kv-handoff-bound) for requests this engine prefills |

`serving` fields:

| Field                      | Meaning                                                       |
| -------------------------- | ------------------------------------------------------------- |
| `http_retained`            | Completion requests counted against the in-flight limit, from arrival until the response ends |
| `http_retained_limit`      | In-flight limit plus `serving.queue_capacity`                 |
| `http_retained_high_water` | Peak number of retained requests                              |
| `prefill_attempts`         | Cumulative prefill dispatches                                 |
| `decode_attempts`          | Cumulative decode dispatches                                  |
| `retry_attempts`           | Prefill attempts after the first                              |
| `retry_credits`            | Available shared retry credit                                 |
| `retry_credits_spent`      | Shared retry credit consumed                                  |
| `retry_denied`             | Retries that the retry budget refused                         |
| `served_after_retry`       | Completed requests whose final attempt followed a failed one  |
| `attempt_failures`         | Failed attempts as `phase`, `reason`, and `count` rows        |
| `decode_tokens_observed`   | Decode tokens the router observed                             |
| `upstream_seconds`         | Total time in engine HTTP calls by phase, across all attempts |

## Scheduler and controller state

### Pool and SLO fields

These objects carry the pool, load, SLO, and floor state:

| Object           | Fields                                                                            |
| ---------------- | --------------------------------------------------------------------------------- |
| `pools`          | `prefill` and `decode` arrays of engine IDs                                       |
| `http_pools`     | `data_connections`, `control_connections`, `pool_timeout_s`                       |
| `load`           | `prefill` and `decode` load as floats relative to the SLO                         |
| `thresholds`     | `expand`, `shrink`, `cooldown_s`, `sustained_intervals`, `dwell_s`, `panic_ratio` |
| `slo`            | `ttft_s`, `tpot_s`                                                                |
| `resident.<iid>` | `prefill` and `decode` in-flight counts                                           |
| `below_floor`    | `active`, `live_prefill`, `since`, `breaches`, `cumulative_s`                     |
| `decode_floor`   | `min_decode`, `live_decode`, `below_floor`, `restoration_moves`                   |

The `below_floor` and `decode_floor` fields hold these values:

| Field                      | Meaning                                                           |
| -------------------------- | ----------------------------------------------------------------- |
| `below_floor.active`       | `true` during a prefill-floor breach                              |
| `below_floor.live_prefill` | Prefill engines eligible for placement                            |
| `below_floor.since`        | Start time of the open breach on the router's monotonic clock     |
| `below_floor.breaches`     | Breaches since the live prefill count first reached `min_prefill` |
| `below_floor.cumulative_s` | Total seconds in breach, across closed and open breaches          |
| `decode_floor.live_decode` | Decode engines eligible for placement                             |

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

When demand history is incomplete or fleet health changes before scoring, `control.last_decision` holds the inputs available at that stage. Depending on the stage, it can carry these fields:

| Field                                             | Meaning                                                                                                                                                           |
| ------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `current_prefill`, `current_decode`               | Current prefill and decode split                                                                                                                                  |
| `prefill_work`, `decode_work`                     | Estimated prefill and decode demand over the [demand spans](#demand-spans), in engines                                                                            |
| `arrivals`                                        | Arrivals in the prefill demand span                                                                                                                               |
| `output_observations`                             | Completed-output observations in demand history                                                                                                                   |
| `demand_complete`                                 | Whether demand over `controller.reactive.window_s` is [complete](06-SLO-and-Demand.md#demand-completeness)                                                        |
| `arrivals_beyond_profile`                         | Offers in `controller.reactive.window_s` with a prompt longer than some engine profile's prefill sweep                                                            |
| `unsized_offers`                                  | [Unsized offers](06-SLO-and-Demand.md#unsized-offers) in `controller.reactive.window_s`                                                                           |
| `projected_ttft_ratio`                            | Demand model's projected TTFT for the candidate split, as a ratio to the TTFT target                                                                              |
| `projected_tpot_ratio`                            | Demand model's projected TPOT for the candidate split, as a ratio to the TPOT target                                                                              |
| `projected_decode_wait_ratio`                     | Candidate split's [decode queueing ratio](../configuration/02-Serving-and-Role-Control.md#75-adjacent-split-decisions)                                            |
| `objective`                                       | Candidate split's objective                                                                                                                                       |
| `objective_delta`                                 | Current objective minus candidate objective, positive for an improvement                                                                                          |
| `decode_request_limit`                            | Applied decode request limit                                                                                                                                      |
| `decode_profile_covered`                          | Whether decode profiles cover the candidate split                                                                                                                 |
| `decode_correction`                               | Fleet median of each engine's bounded ratio of live to profiled decode latency                                                                                    |
| `decision_basis`                                  | `demand_projection`, `prefill_pressure_recovery`, `decode_pressure_recovery`, or `projected_ttft_recovery`                                                        |
| `observed_prefill_ratio`, `observed_decode_ratio` | Observed phase pressure                                                                                                                                           |
| `recovery_prefill_ratio`, `queued_prefill_s`      | The [prefill recovery ratio](06-SLO-and-Demand.md#prefill-recovery-ratio) and its queued prefill seconds                                                          |
| `recovery_decode_ratio`                           | Value of the [decode recovery ratio](06-SLO-and-Demand.md#decode-recovery-ratio)                                                                                  |
| `eligibility_rule`                                | Rule applied to a scored proposal                                                                                                                                 |
| `demand_horizon_s`                                | Demand span that priced the proposal, in seconds                                                                                                                  |
| `steady_horizon_s`                                | Confirmation span, in seconds                                                                                                                                     |
| `steady_prefill_work`, `steady_decode_work`       | Prefill and decode demand over `steady_horizon_s` in engines, `null` with incomplete demand or on a projected-TTFT recovery evaluation                            |
| `steady_demand_s`                                 | Seconds since confirmation-span demand began to match window demand, `null` while they differ, with incomplete demand, or on a projected-TTFT recovery evaluation |
| `departure_age_s`                                 | Seconds since the open departure began                                                                                                                            |
| `departure_reverses`                              | Whether the open departure reverses the role controller's recent moves                                                                                            |
| `confirmations`, `required_confirmations`         | Consecutive confirmations of an eligible proposal and the required count                                                                                          |
| `decode_capacity_safe`                            | Whether the candidate's decode work fits its decode capacity                                                                                                      |
| `role_floors_safe`                                | Whether the candidate respects `min_prefill` and `min_decode`                                                                                                     |
| `source_pressure_safe`                            | Whether source-pool pressure is at or below `shrink`, or whether `mixed_pressure` or `steady_demand` applies                                                      |

These decisions add fields to `control.last_decision`:

| Decision                        | Added fields                                                                                                                                                                                       |
| ------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Scored                          | Decode capacity fields `decode_tokens_per_engine`, `decode_slo_capacity_tokens`, `decode_kv_capacity_tokens`, `decode_requests_per_engine`, `pending_decode_requests`, and `pending_decode_tokens` |
| Scored                          | Demand span fields `demand_horizon_s`, `steady_horizon_s`, `steady_prefill_work`, `steady_decode_work`, and `steady_demand_s`                                                                      |
| Scored during an open departure | `departure_age_s` and `departure_reverses`                                                                                                                                                         |
| Decode-to-prefill               | `risk_kind`, `risk_age_s`, and the [`demand_evidence`](06-SLO-and-Demand.md#consolidation-evidence) fields with an `evidence_` prefix                                                              |

`eligibility_rule` takes the first value that applies, in table order:

| `eligibility_rule`        | Proposal                                                                                                                              |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `projected_ttft_recovery` | Urgent decode-to-prefill evaluation triggered by an arriving request                                                                  |
| `mixed_pressure`          | Observed prefill recovery exceeds the decode shrink threshold                                                                         |
| `settled_departure`       | A [departure's](../concepts/02-Role-Control.md#departures-from-a-settled-split) first move, priced on confirmation-span demand        |
| `steady_demand`           | [Steady-demand](../concepts/02-Role-Control.md#steady-demand) move with projected source load above `shrink` and at or below `expand` |
| `source_shrink`           | Ordinary consolidation, and a leading departure's later confirmation-span moves                                                       |

#### Demand spans

`demand_horizon_s`, `prefill_work`, `arrivals` and `decode_work` cover these spans:

| Decision                                                                                               | `demand_horizon_s`                                                                               | `prefill_work`, `arrivals`     | `decode_work`                                                                                                   |
| ------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------ | ------------------------------ | --------------------------------------------------------------------------------------------------------------- |
| Hold before scoring                                                                                    |                                                                                                  | `controller.reactive.window_s` | `controller.reactive.window_s`                                                                                  |
| A [departure's](../concepts/02-Role-Control.md#departures-from-a-settled-split) confirmation-span move | [Confirmation span](../configuration/02-Serving-and-Role-Control.md#75-adjacent-split-decisions) | Confirmation span              | Larger of residency over the latest `controller.reactive.step_s` and expected decode over the confirmation span |
| Other decode-to-prefill proposal                                                                       | `controller.reactive.window_s`                                                                   | `controller.reactive.window_s` | Larger of the `controller.reactive.evidence_span_s` and `controller.reactive.window_s` estimates                |
| Every other scored decision                                                                            | `controller.reactive.window_s`                                                                   | `controller.reactive.window_s` | `controller.reactive.window_s`                                                                                  |

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

A scored candidate that moves one engine from decode to prefill adds:

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

| Field              | Meaning                                                                          |
| ------------------ | -------------------------------------------------------------------------------- |
| `at`               | Role-change time on the router's monotonic clock                                 |
| `iid`              | Engine ID                                                                        |
| `to`               | New role: `prefill` or `decode`                                                  |
| `by`               | Caller that changed the role                                                     |
| `prefill_inflight` | Resident prefill work when the role label changes                                |
| `decode_inflight`  | Resident decode work when the role label changes                                 |
| `drained_s`        | Seconds from the role change until its resident work finishes, `null` until then |

`by` values:

| `by`             | Role change                                                                           |
| ---------------- | ------------------------------------------------------------------------------------- |
| `reactive`       | Role-controller move                                                                  |
| `decode_floor`   | Decode-floor restoration toward `min_decode`                                          |
| `floor_recovery` | Prefill-floor recovery, moving healthy decode engines to prefill down to `min_decode` |

Each `flips_refused[]` record contains `at`, `to`, and `why`.
