# Live router and scheduler state

## Live router state

### `GET /narwhal/state`

Narwhal returns live scheduler and router state as `narwhal.state` schema version `1`.

### Top-level state

| Field                   | Meaning                                                                              |
| ----------------------- | ------------------------------------------------------------------------------------ |
| `schema`                | `narwhal.state`                                                                      |
| `schema_version`        | State schema version; this release writes `1`                                        |
| `served`                | Completed requests; preserved across resume and takeover                             |
| `failed`                | Requests ending in error; preserved across resume and takeover                       |
| `offered`               | Completion arrivals in the current process                                           |
| `unsized_offered`       | Arrivals terminating before workload sizing                                          |
| `expired`               | Deadline expiries in the current process                                             |
| `cancelled`             | Client disconnects; preserved across resume and takeover                             |
| `invalid_requests`      | Malformed requests rejected before admission in the current run                      |
| `controller`            | Active role controller; always `reactive`                                            |
| `token_accounting`      | `token_ids` for exact token identity, otherwise `unavailable`                        |
| `control`               | Controller mode, decisions, flips, refusals, reversals, and role-change accounting   |
| `monitoring`            | Monitoring-loop timing and failure state                                             |
| `ha`                    | Readiness, standby state, lease epoch and holder, plus fencing reason                |
| `lifecycle`             | Drain state, resident work, identities, validation, and retained lifecycle events    |
| `admission`             | Router and phase occupancy, queue state, limits, rejections, and predictive refusals |
| `serving`               | Retained HTTP work, attempts, retry state, observed tokens, and upstream time        |
| `http_pools`            | Data/control connection pools and pool-wait timeout                                  |
| `pools`                 | Engines grouped by prefill or decode role                                            |
| `load`                  | Per-pool SLO-relative load; `1.0` equals the configured target                       |
| `thresholds`            | Active reactive-controller thresholds                                                |
| `slo`                   | TTFT and TPOT targets                                                                |
| `first_token_timeout_s` | Decode first-token deadline                                                          |
| `resident`              | In-flight prefill and decode work by engine                                          |
| `pinned`                | Engines excluded from role changes                                                   |
| `min_prefill`           | Configured minimum live prefill count                                                |
| `min_decode`            | Configured minimum live decode count                                                 |
| `below_floor`           | Current and cumulative prefill-floor breach state                                    |
| `ejected`               | Engines removed by the breaker                                                       |
| `draining`              | Engines excluded by lifecycle action                                                 |
| `probation`             | Engines carrying a predictive-health placement penalty                               |
| `health`                | Per-engine drift-window accounting                                                   |
| `quarantined`           | Engines temporarily excluded after engine failure                                    |
| `breaker`               | Per-engine consecutive failure streaks and probe state                               |
| `decode_floor`          | Decode floor state and restoration count                                             |
| `attainment`            | Bounded diagnostic SLO outcome buckets                                               |
| `demand_history`        | Retained demand, shape counts, and overflow state                                    |
| `demand_evidence`       | Consolidation evidence used by decode-to-prefill gates                               |
| `unserved`              | Phase placements for which every eligible candidate exceeded configured SLO          |
| `panic_bypasses`        | Prefill-to-decode moves allowed through cooldown by panic logic                      |
| `flips_refused`         | The 20 most recent rejected role changes                                             |
| `flips`                 | Role changes retained up to `flip_history`                                           |

#### `health`

Each engine's health record contains drift-window accounting for:

- scored closed windows
- undersampled closed windows
- observations
- `last_scored_s_ago`

`prefill_paused` indicates that evidence collection is suspended because of local prefill interference.

`prefill_pauses` counts transitions into that condition.

Confirmed ejection clears the engine's drift-window record, so readmission starts a fresh health window.

#### `breaker`

Failure streaks are maintained separately for:

- `connection`
- `timeout`
- `overload`
- `inference_status`
- `kv_handoff`
- `stream`
- `liveness`

The record also identifies engines with a health or inference probe currently in flight.

---

## Admission and serving state

The `admission` object exposes:

| Field              | Meaning                                                    |
| ------------------ | ---------------------------------------------------------- |
| `inflight`         | Requests holding a router admission seat                   |
| `queued`           | Current admission queue depth                              |
| `queue_capacity`   | Configured queue bound                                     |
| `queue_high_water` | Peak queue depth                                           |
| `waiting_prefill`  | Requests waiting for prefill dispatch                      |
| `waiting_decode`   | Requests waiting for decode dispatch                       |
| `limit`            | Effective router limit after `--max-concurrent` precedence |
| `rejected`         | Global capacity rejections                                 |
| `refused`          | Global predictive-admission refusals                       |
| `engine_auth`      | `boundary` or `engine-credential`                          |

The `serving` object exposes:

| Field                      | Meaning                                                      |
| -------------------------- | ------------------------------------------------------------ |
| `http_retained`            | Completion requests currently retaining HTTP resources       |
| `http_retained_limit`      | Configured retained-request limit                            |
| `http_retained_high_water` | Peak retained-request occupancy                              |
| `prefill_attempts`         | Cumulative prefill dispatches                                |
| `decode_attempts`          | Cumulative decode dispatches                                 |
| `retry_attempts`           | Additional prefill attempts                                  |
| `retry_credits`            | Shared retry credit currently available                      |
| `retry_credits_spent`      | Shared retry credit consumed                                 |
| `retry_denied`             | Retries refused by budget                                    |
| `decode_tokens_observed`   | Observed decode tokens                                       |
| `upstream_seconds`         | Cumulative HTTP leg time by phase, including failed attempts |

---

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

Narwhal checks each proposed role move against `min_prefill` and `min_decode`, using engines eligible for placement as the live count. An ejection or operator hold can lower that count independently of role control. When prefill falls below its floor, the controller moves healthy decode engines into prefill while preserving `min_decode`; `below_floor.live_prefill` reports the eligible prefill count through that recovery.

Aggregate mode treats an initial zero-prefill pool as its baseline.

After prefill first reaches `min_prefill`, Narwhal records the process-monotonic time in `below_floor.since` on each later drop, marks `active`, and clears both fields when the pool recovers.

While a breach is active, `below_floor.cumulative_s` includes the open interval.

### Controller decisions

`control` exposes:

- `advisory`
- `last_decision`
- cumulative `decisions` grouped by caller and result

`control.last_decision` may contain:

- current split
- proposed split
- demand reference
- phase work
- projected SLO ratios
- applied decode request limit
- decode profile coverage
- decode correction
- objective change
- reason
- result

Decode-to-prefill decisions also include the consolidation-evidence snapshot.

Scored reactive decisions report `eligibility_rule` using one of:

```text
source_shrink
mixed_pressure
projected_ttft_recovery
```

Meanings:

- `source_shrink`: ordinary consolidation
- `mixed_pressure`: observed prefill recovery exceeds the decode shrink threshold
- `projected_ttft_recovery`: arrival-triggered decode-to-prefill evaluation

Eligible proposals also report:

```text
confirmations
required_confirmations
```

#### Projected-TTFT recovery fields

A projected-TTFT recovery record includes:

```text
trigger_rid
projected_ttft_s
ttft_slo_s
trigger_projected_ttft_ratio
resident_prefill_s
queued_prefill_s
waiting_prefill
initial_projected_ttft_s
urgent_signals
event_to_evaluation_s
```

A scored adjacent candidate adds:

```text
candidate_projected_ttft_s
candidate_projected_ttft_ratio
projected_ttft_improvement_s
decode_capacity_safe
role_floors_safe
source_pressure_safe
```

Scored projected-TTFT recovery decisions set `decision_basis=projected_ttft_recovery`.

`projected_ttft_ratio` continues to represent the demand model's ratio for the candidate split.

Blocked and held decisions retain:

- proposed split
- objective change
- the constraint that prevented movement

Possible constraints include:

- consolidation evidence
- profile coverage
- KV capacity
- role floors
- pins
- cooldown
- dwell
- resident guard

For insufficient demand history or a fleet-health change before candidate scoring, `control.last_decision` records the inputs available at that stage.

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

`by` identifies the caller:

- `reactive`
- `decode_floor`
- `floor_recovery`

`prefill_inflight` and `decode_inflight` record resident work at the point when the role label changes.

`drained_s` is populated with the drain duration when that resident work finishes.

Each `flips_refused[]` record contains:

```text
at
to
why
```

The [request journal](../telemetry/01-Journal.md#diagnose-a-request-from-the-journal) records per-request evidence.
