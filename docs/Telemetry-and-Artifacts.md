# Telemetry and artifact reference

Narwhal retains request, profile, metric, canary and contract-version records for deployment inspection and compatibility checks.

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
| Contract identity   | `narwhal_contract_info`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
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

Pool load uses the phase-specific normalization defined in [Role control](Configuration.md#role-control). A value of `1.0` reaches the phase target.

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
