# Router state (`/narwhal/state`)

## Live router state

### `GET /narwhal/state`

Returns the router's and scheduler's live state as a `narwhal.state` document, schema version `1`.

### Top-level state

| Field                   | Meaning                                                                            |
| ----------------------- | ---------------------------------------------------------------------------------- |
| `schema`                | Always `narwhal.state`                                                             |
| `schema_version`        | `1` in this release                                                                |
| `journal_run`           | ID of the current router process, as used in request journal rows                  |
| `served`                | Completed requests; carried over on resume and takeover                            |
| `failed`                | Requests that ended in an error; carried over on resume and takeover               |
| `offered`               | Completion arrivals since this process started                                     |
| `unsized_offered`       | Arrivals that ended before the workload was sized                                  |
| `expired`               | Deadline expiries since this process started                                       |
| `cancelled`             | Client disconnects; carried over on resume and takeover                            |
| `invalid_requests`      | Malformed requests rejected before admission in this run                           |
| `controller`            | The active role controller, which is always `reactive`                             |
| `token_accounting`      | `token_ids` when exact token identity is available, otherwise `unavailable`        |
| `control`               | Controller mode, decisions, flips, refusals, reversals, and role-change accounting |
| `monitoring`            | Monitoring-loop timing and failures                                                |
| `ha`                    | Readiness, standby state, lease epoch and holder, and why the router was fenced    |
| `lifecycle`             | Drain state, resident work, identities, validation, and recent lifecycle events    |
| `admission`             | Router and phase occupancy, queue, limits, rejections, and predictive refusals     |
| `serving`               | Retained HTTP work, attempts, retries, observed tokens, and upstream time          |
| `http_pools`            | Data and control connection pools, and the pool-wait timeout                       |
| `pools`                 | Engines grouped by role, prefill or decode                                         |
| `load`                  | Load per pool relative to the SLO; `1.0` is exactly on target                      |
| `thresholds`            | The reactive controller's current thresholds                                       |
| `slo`                   | TTFT and TPOT targets                                                              |
| `first_token_timeout_s` | Deadline for the first decode token                                                |
| `resident`              | In-flight prefill and decode work on each engine                                   |
| `pinned`                | Engines that can't change role                                                     |
| `min_prefill`           | Configured minimum number of live prefill engines                                  |
| `min_decode`            | Configured minimum number of live decode engines                                   |
| `below_floor`           | Whether prefill is under its floor now, and how often and how long it has been     |
| `ejected`               | Engines the breaker has removed                                                    |
| `draining`              | Engines held out by a lifecycle action                                             |
| `probation`             | Engines placed with a predictive-health penalty                                    |
| `health`                | Drift-window accounting for each engine                                            |
| `quarantined`           | Engines briefly held out after an engine failure                                   |
| `breaker`               | Consecutive failure streaks and probe state for each engine                        |
| `decode_floor`          | Decode floor state and how many restoration moves it has made                      |
| `attainment`            | Bounded diagnostic buckets of SLO outcomes                                         |
| `demand_history`        | Retained demand, shape counts, and overflow                                        |
| `demand_evidence`       | The consolidation evidence behind decode-to-prefill gates                          |
| `unserved`              | Phase placements where every eligible candidate was over the configured SLO        |
| `panic_bypasses`        | Prefill-to-decode moves that panic logic let through during cooldown               |
| `flips_refused`         | The last 20 rejected role changes                                                  |
| `flips`                 | Role changes, keeping up to `flip_history`                                         |

#### `health`

Each engine's `health` record tracks its drift windows: how many closed windows were `scored`, how many closed `undersampled`, and `last_scored_s_ago`.

`prefill_paused` shows whether evidence collection is suspended because local prefill work is interfering with it, and `prefill_pauses` counts how many times collection has entered that state. A confirmed ejection wipes the record, so an engine that gets readmitted starts with a clean health window.

#### `breaker`

Failure streaks are kept separately for each kind of fault: `connection`, `timeout`, `overload`, `inference_status`, `kv_handoff`, `stream`, and `liveness`. The record also shows which engines have a health or inference probe in flight.

## Admission and serving state

`admission` reports:

| Field              | Meaning                                                     |
| ------------------ | ----------------------------------------------------------- |
| `inflight`         | Requests holding a router admission seat                    |
| `queued`           | Current depth of the admission queue                        |
| `queue_capacity`   | Configured queue bound                                      |
| `queue_high_water` | Deepest the queue has been                                  |
| `waiting_prefill`  | Requests waiting for prefill dispatch                       |
| `waiting_decode`   | Requests waiting for decode dispatch                        |
| `limit`            | Effective router limit, after `--max-concurrent` is applied |
| `rejected`         | Capacity rejections, global                                 |
| `refused`          | Predictive-admission refusals, global                       |
| `engine_auth`      | `boundary` or `engine-credential`                           |

`serving` reports:

| Field                      | Meaning                                                                       |
| -------------------------- | ----------------------------------------------------------------------------- |
| `http_retained`            | Completion requests currently holding HTTP resources                          |
| `http_retained_limit`      | Limit on retained requests: `admission.limit` plus `admission.queue_capacity` |
| `http_retained_high_water` | Most retained requests at once                                                |
| `prefill_attempts`         | Prefill dispatches so far                                                     |
| `decode_attempts`          | Decode dispatches so far                                                      |
| `retry_attempts`           | Extra prefill attempts from retries                                           |
| `retry_credits`            | Shared retry credit available now                                             |
| `retry_credits_spent`      | Shared retry credit used so far                                               |
| `retry_denied`             | Retries turned down because the budget ran out                                |
| `decode_tokens_observed`   | Decode tokens seen                                                            |
| `upstream_seconds`         | Total HTTP leg time per phase, failed attempts included                       |

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

Every proposed role move has to respect `min_prefill` and `min_decode`. "Live" here means eligible for placement, so an ejection or an operator hold can push a pool under its floor without any role change at all. If prefill drops below its floor, the controller moves healthy decode engines over to prefill as long as decode stays at or above `min_decode`. While that recovery is underway, `below_floor.live_prefill` shows how many prefill engines are eligible.

In aggregate mode, a fleet that starts with no prefill engines treats that as its baseline.

Floor tracking begins the first time prefill reaches `min_prefill`. After that, each drop below the floor sets `active` and records the process-monotonic time in `below_floor.since`. Both clear when the pool recovers. `below_floor.cumulative_s` includes the current breach while it's still open.

### Controller decisions

`control` holds `advisory`, `last_decision`, and these process-lifetime counts:

- `decisions`, keyed by `caller:result`
- `flips`, applied role changes keyed by `caller:role`
- `flip_reversals`, role changes that undid an engine's previous change
- `flips_refused`, the total number of refused role changes
- `flip_inflight`, the prefill and decode work that was resident on engines when they changed role

`control.last_decision` describes the most recent decision. Depending on how far it got, it can include the current and proposed splits, the demand reference, phase work, projected SLO ratios, the decode request limit that was applied, decode profile coverage, decode correction, the change in objective, the reason, and the result. Decode-to-prefill decisions also carry a snapshot of the consolidation evidence.

Scored reactive decisions record which rule made them eligible in `eligibility_rule`:

| `eligibility_rule`        | When it applies                                                |
| ------------------------- | -------------------------------------------------------------- |
| `source_shrink`           | Ordinary consolidation                                         |
| `mixed_pressure`          | Observed prefill recovery is above the decode shrink threshold |
| `projected_ttft_recovery` | A decode-to-prefill evaluation triggered by an arrival         |

Eligible proposals also report `confirmations` and `required_confirmations`.

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

The candidate it scores is the neighboring split, with one more prefill engine and one fewer decode engine than now. Scoring that candidate adds:

```text
candidate_projected_ttft_s
candidate_projected_ttft_ratio
projected_ttft_improvement_s
decode_capacity_safe
role_floors_safe
source_pressure_safe
```

These decisions set `decision_basis=projected_ttft_recovery`. `projected_ttft_ratio` keeps its usual meaning, the demand model's ratio for the candidate split.

A blocked or held decision keeps its proposed split and objective change, along with the constraint that stopped it: consolidation evidence, profile coverage, KV capacity, role floors, pins, cooldown, dwell, or the resident guard. If a decision stops before candidates are scored, because there isn't enough demand history or fleet health changed, `last_decision` holds whatever inputs existed at that point.

### Role-change history

Each role change is recorded like this:

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

`by` is the caller that made the change: `reactive`, `decode_floor`, or `floor_recovery`. `prefill_inflight` and `decode_inflight` are the resident work on the engine at the moment its role label changed, and `drained_s` is filled in with how long that work took to finish.

Refused changes go in `flips_refused[]`, each with `at`, `to`, and `why`.

For evidence about individual requests, see the [request journal](../telemetry/01-Journal.md#diagnose-a-request-from-the-journal).
