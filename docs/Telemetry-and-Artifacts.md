# Telemetry and artifact reference

Narwhal records requests, profiles, metrics, and contract versions for deployment inspection and compatibility checks.

## Request journal

Narwhal writes request timing and placement data as JSON Lines. `--journal <path>` sets the journal file. Without it, Narwhal writes `journal.jsonl` beside `profiles.path`. The file is append-only; `run` distinguishes records written by different router processes.

The first row identifies the schema, package version, source build, and token-accounting mode. `source` is a SHA-256 digest of the installed package's Python files, so the same source produces the same digest from a checkout, deployment, or wheel.

```json
{"meta":{"schema":"narwhal.journal","schema_version":1,"package":"narwhal-inference","version":"0.1.0","git":"<commit>","source":"sha256:...","token_accounting":"token_ids"}}
```

One terminal row is written for every original completion request, including invalid requests, capacity refusals, expiries, and cancellations. Retries remain attached to that original row as attempts.

| Field                                        | Meaning                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| -------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `run`, `rid`, `client_rid`                   | Router run, Narwhal request ID, and optional caller request ID.                                                                                                                                                                                                                                                                                                                                                                            |
| `arrived`                                    | Arrival time on the process monotonic clock. Compare only within the same run.                                                                                                                                                                                                                                                                                                                                                             |
| `input_len`, `output_len`, `wanted_len`      | Prompt size, returned size, and requested output size. For cancelled requests, `output_len` is the number of delivered tokens when measurable.                                                                                                                                                                                                                                                                                             |
| `ttft_s`, `tpot_s`, `first_byte_s`           | Router-side prefill, decode, and first-visible-output timing.                                                                                                                                                                                                                                                                                                                                                                              |
| `prefill_iid`, `decode_iid`                  | Engines selected for the prefill and decode legs.                                                                                                                                                                                                                                                                                                                                                                                          |
| `crossed`                                    | Whether decode consumed KV produced by the recorded prefill engine.                                                                                                                                                                                                                                                                                                                                                                        |
| `token_accounting`                           | Decode-output accounting mode. `token_ids` means exact per-token identity; all other dialects report `unavailable`.                                                                                                                                                                                                                                                                                                                        |
| `refused`, `refused_cause`                   | Predictive refusal and the priced cause.                                                                                                                                                                                                                                                                                                                                                                                                   |
| `cancelled`, `cancelled_phase`               | Client disconnect during `admission`, `queue`, `backoff`, `prefill`, or `decode`.                                                                                                                                                                                                                                                                                                                                                          |
| `terminal`                                   | Final state: `completed`, `failed`, `refused`, `rejected`, `expired`, `invalid`, or `cancelled`.                                                                                                                                                                                                                                                                                                                                           |
| `input_sized`                                | Whether local sizing completed. If false, the body terminated before sizing and `input_len: 0` records the early exit.                                                                                                                                                                                                                                                                                                                     |
| `attempts`, `decode_attempts`                | Prefill and decode dispatch counts for the original request.                                                                                                                                                                                                                                                                                                                                                                               |
| `attempt_failures`                           | Bounded details for failed attempts, including failures followed by successful retries. Each entry records monotonic time, attempt number, phase, engine IDs, exception type/message/status, transient classification, visible-output state, retry decision, and scheduled backoff. The list is capped at `serving.max_attempts`; messages are truncated to 240 characters. A retry scheduled here may still be cancelled before dispatch. |
| `queue_wait_s`, `duration_s`                 | Total admission/dispatch wait and complete request lifetime on the router clock.                                                                                                                                                                                                                                                                                                                                                           |
| `decode_tpot_s`                              | Time from first to last observed output token divided by `output_tokens - 1`. Null for fewer than two tokens or when exact token accounting is unavailable.                                                                                                                                                                                                                                                                                |
| `decode_tokens_observed`, `upstream_seconds` | Tokens observed across all attempts and summed HTTP-leg durations, including failed work, transfer time, and waiting.                                                                                                                                                                                                                                                                                                                      |
| `error`                                      | Failure or refusal detail. Null for successful and cancelled requests.                                                                                                                                                                                                                                                                                                                                                                     |

Cancelled rows keep any measured timing and use `error: null`. Journal attainment scoring excludes cancellations and predictive refusals. Invalid requests, capacity rejections, expiries, and engine failures count as misses. Completion counters also exclude cancellations.

A terminal request with null `output_len` or `ttft_s` is a miss for attainment scoring. Refusals, cancellations, and errors retain their own outcome categories.

`first_byte_s - ttft_s` measures KV transfer plus decode queueing. Crossed requests normally incur both components. Local decode can still queue.

The same journal also stores event rows. Current event types cover role-floor breaches and recoveries, blocked decode-floor changes, engine lifecycle operations, and monitoring health. Monitoring emits:

- `monitoring_stage_failure`, with `stage`, `class`, and the stage-local `consecutive` failure count
- `monitoring_degraded` when failures reach `controller.monitor_failure_limit`
- `monitoring_recovered` after a fully successful pass clears degraded state

Request analysis selects terminal request rows and processes event rows separately.

Failure strings may contain engine IDs and engine URLs. Remove those identifiers before publishing timing journals.

## Profile store

`narwhal-profile` writes one cost-model row per engine to `profiles.path`. The file is a versioned document with `"schema": "narwhal.profiles"`, `schema_version: 1`, and a `profiles` list. Readers require the declared structure, measured decode bounds, and error evidence.

Router startup and preflight use the same validator. A malformed row aborts the operation with the file, engine, and field in the error, for example:

`profiles.json: profile n4: tpot_slope must be positive`

The store must contain exactly the configured engine set. Missing and extra rows are rejected, with the affected engine IDs listed.

When engines are profiled separately with `narwhal-profile --only`, combine the measured rows into one complete store before preflight or serving.

| Field                                          | JSON type         | Rule                                                                                                             |
| ---------------------------------------------- | ----------------- | ---------------------------------------------------------------------------------------------------------------- |
| `iid`                                          | string            | Nonempty.                                                                                                        |
| `ttft_a`, `ttft_b`, `ttft_c`                   | number            | Nonnegative prefill quadratic coefficients.                                                                      |
| `tpot_slope`                                   | number            | Strictly positive decode interval per resident KV token. A zero slope would price decode capacity as infinite.   |
| `tpot_intercept`                               | number            | Nonnegative zero-contention decode interval.                                                                     |
| `kv_capacity_tokens`                           | integer, optional | Positive when present. If `decode_max_kv_tokens` is also present, physical capacity must be at least that large. |
| `tpot_request_slope`                           | number            | Nonnegative decode interval per active sequence. Defaults to `0`.                                                |
| `decode_min_requests`, `decode_max_requests`   | integer           | Positive measured concurrency range, with min ≤ max.                                                             |
| `decode_min_kv_tokens`, `decode_max_kv_tokens` | integer           | Positive measured resident-KV range, with min ≤ max.                                                             |
| `decode_fit_mape`, `decode_cv_mape`            | number            | Nonnegative fit error and leave-one-out cross-validation error.                                                  |

JSON type checks are strict. Integer fields reject `true`, `"96"`, and `1.5`; number fields accept both integer and floating-point JSON numbers. All numeric values must be finite. `NaN` and `Infinity` are rejected before row parsing.

For each fitted engine, decode concurrency is capped by the smaller of:

1. `decode_max_requests`
2. the number of requests that fit the KV budget at the priced context length

The KV budget starts from `decode_max_kv_tokens` and is further capped by physical `kv_capacity_tokens` when supplied. If the measured request bound cannot be used, decode request capacity is zero.

## Metrics

Six request-outcome counters survive resume and standby takeover:

`narwhal_served_total`, `narwhal_failed_total`, `narwhal_unserved_total`, `narwhal_refused_total`, `narwhal_rejected_total`, and `narwhal_cancelled_total`.

The following state is process-local and resets on restart: offered, unsized, and expired counters; attempt counters; retry quota; histograms; controller decision and role-change counters; floor counters; and the invalid-request counter.

When reconciling journals and Prometheus data, use `run` as the journal process boundary. Cumulative outcome counters and process-local offered counters do not share the same restart semantics.

| Group               | Series                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Contract identity   | `narwhal_contract_info`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| Router ownership    | `narwhal_router_ready`, `narwhal_router_lease_epoch`                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| Monitoring          | `narwhal_monitoring_degraded`, `narwhal_monitoring_core_consecutive_failures`, `narwhal_monitoring_core_failures_total`, `narwhal_monitoring_stage_failures_total`, `narwhal_monitoring_stage_consecutive_failures`, `narwhal_event_loop_lag_seconds`, `narwhal_event_loop_lag_high_water_seconds`                                                                                                                                                                                                                        |
| Request outcomes    | `narwhal_offered_total`, `narwhal_unsized_offered_total`, `narwhal_expired_total`, `narwhal_served_total`, `narwhal_failed_total`, `narwhal_unserved_total`, `narwhal_refused_total`, `narwhal_rejected_total`, `narwhal_cancelled_total`, `narwhal_invalid_requests_total`                                                                                                                                                                                                                                               |
| Attempts and quota  | `narwhal_prefill_attempts_total`, `narwhal_decode_attempts_total`, `narwhal_retry_attempts_total`, `narwhal_retry_credits`, `narwhal_retry_credits_spent_total`, `narwhal_retry_denied_total`, `narwhal_decode_tokens_observed_total`, `narwhal_upstream_seconds_total`                                                                                                                                                                                                                                                   |
| Queueing            | `narwhal_queued`, `narwhal_queue_capacity`, `narwhal_queue_high_water`, `narwhal_waiting_prefill`, `narwhal_waiting_decode`, `narwhal_queue_wait_seconds`                                                                                                                                                                                                                                                                                                                                                                 |
| HTTP retention      | `narwhal_http_retained`, `narwhal_http_retained_limit`, `narwhal_http_retained_high_water`                                                                                                                                                                                                                                                                                                                                                                                                                                |
| Pools               | `narwhal_pool_instances`, `narwhal_pool_load`, `narwhal_instance_role`, `narwhal_resident_requests`                                                                                                                                                                                                                                                                                                                                                                                                                       |
| Health              | `narwhal_ejected_instances`, `narwhal_ejected`, `narwhal_probation_instances`, `narwhal_health_windows_scored_total`, `narwhal_health_windows_undersampled_total`, `narwhal_health_prefill_paused`, `narwhal_health_prefill_pauses_total`, `narwhal_engine_breaker_streak`, `narwhal_engine_breaker_verifying`                                                                                                                                                                                                            |
| Floors              | `narwhal_prefill_below_floor`, `narwhal_decode_floor`, `narwhal_decode_below_floor`, `narwhal_prefill_below_floor_events_total`, `narwhal_prefill_below_floor_seconds_total`, `narwhal_decode_floor_restorations_total`                                                                                                                                                                                                                                                                                                   |
| Controller          | `narwhal_flips_total`, `narwhal_flip_reversals_total`, `narwhal_flips_refused_total`, `narwhal_flip_inflight_total`, `narwhal_controller_advisory`, `narwhal_controller_decisions_total`, `narwhal_controller_proposed_engines`, `narwhal_controller_phase_work_engines`, `narwhal_controller_projected_slo_ratio`, `narwhal_controller_objective`, `narwhal_controller_decode_tokens_per_engine`, `narwhal_controller_decode_requests_per_engine`, `narwhal_controller_decode_model`, `narwhal_controller_last_decision` |
| Demand history      | `narwhal_demand_history_cells`, `narwhal_demand_history_cell_limit`, `narwhal_demand_history_observations`, `narwhal_demand_history_overflow_observations`                                                                                                                                                                                                                                                                                                                                                                |
| Attainment evidence | `narwhal_attainment_evidence_covered_seconds`, `narwhal_attainment_evidence_outcomes`, `narwhal_attainment_evidence_buckets`, `narwhal_attainment_evidence_pruned_total`                                                                                                                                                                                                                                                                                                                                                  |
| Latency             | `narwhal_slo_seconds`, `narwhal_ttft_seconds`, `narwhal_tpot_seconds`, `narwhal_seat_seconds`                                                                                                                                                                                                                                                                                                                                                                                                                             |
| Lifecycle           | `narwhal_engine_draining`, `narwhal_engine_ready_to_stop`                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |

Role-change counters begin accumulating when the scheduler starts. `narwhal_flip_reversals_total` increments when an engine moves back from its previous recorded target role, beginning with the engine's second recorded move. `narwhal_flips_refused_total` covers role-change attempts blocked by timing, availability, pins, floors, the resident guard, or advisory mode. Controller decision metrics record evaluation outcomes.

Pool load follows the phase-specific normalization in [Role control](Configuration.md#role-control). `1.0` means the phase target has been reached.

`narwhal_slo_seconds` exposes configured `ttft` and `tpot` budgets under the `metric` label. TTFT and TPOT histograms use these multiples of the corresponding budget:

`0.025`, `0.05`, `0.1`, `0.2`, `0.35`, `0.5`, `0.7`, `1.0`, `1.5`, `3.0`, `10.0`, `+Inf`

Queue-wait and seat-time histograms use their configured request-lifecycle bounds. Compute quantiles from bucket rates grouped by `instance` and `le`. Aggregating multiple routers is valid only when all routers use identical bucket edges.

The attainment-evidence gauges describe retained controller outcome buckets. `narwhal_attainment_evidence_pruned_total` appears only after evidence older than every consumer horizon has been dropped. Its `kind` label is either `buckets` or `outcomes`.

### Demand history and decode floor

Each scrape exports the configured decode floor and one sample for every retained demand-history window. Restarting a router clears the process-local histories. `narwhal_decode_floor` is read from the new process's fleet configuration.

| Metric                                         | Type  | Labels                                                                             | Value                                                      |
| ---------------------------------------------- | ----- | ---------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| `narwhal_decode_floor`                         | gauge | none                                                                               | Configured `min_decode`.                                   |
| `narwhal_demand_history_cells`                 | gauge | `window`: `unsized`, `arrivals`, `expected_decode`, `observed_decode`, `residency` | Retained demand cohorts.                                   |
| `narwhal_demand_history_cell_limit`            | gauge | same `window` values                                                               | Maximum retained cohorts.                                  |
| `narwhal_demand_history_observations`          | gauge | same `window` values                                                               | Original observations represented by the window.           |
| `narwhal_demand_history_overflow_observations` | gauge | same `window` values                                                               | Observations coalesced after the cohort limit was reached. |

### Consolidation evidence

| Metric                                            | Meaning                                                                                                | Labels                              |
| ------------------------------------------------- | ------------------------------------------------------------------------------------------------------ | ----------------------------------- |
| `narwhal_demand_evidence_span_seconds`            | Duration of the retained arrival window, in seconds.                                                   | none                                |
| `narwhal_demand_evidence_arrivals`                | Arrival samples in that window.                                                                        | none                                |
| `narwhal_demand_evidence_closed`                  | `1` once the evidence window has closed.                                                               | none                                |
| `narwhal_demand_evidence_risk_age_seconds`        | Age of the newest risk event, in seconds.                                                              | none                                |
| `narwhal_demand_evidence_short_decode_engines`    | Short-horizon decode demand, expressed in engine equivalents.                                          | none                                |
| `narwhal_demand_evidence_envelope_decode_engines` | Conservative decode-demand envelope, in engine equivalents.                                            | none                                |
| `narwhal_demand_evidence_trend_ratio`             | Short-horizon demand divided by long-horizon demand. Emitted only when a long-horizon estimate exists. | none                                |
| `narwhal_demand_evidence_refused`                 | `1` for any gate currently blocking consolidation. All gates are `0` when consolidation is allowed.    | `gate`: `risk`, `evidence`, `trend` |
| `narwhal_demand_evidence_risk_events_total`       | Risk-event count. Appears after the first event.                                                       | `kind`                              |



### Monitoring

| Metric                                          | Meaning                                                           | Labels  |
| ----------------------------------------------- | ----------------------------------------------------------------- | ------- |
| `narwhal_monitoring_degraded`                   | `1` while repeated monitoring-pass failures block new admissions. | none    |
| `narwhal_monitoring_core_consecutive_failures`  | Consecutive failed monitoring passes.                             | none    |
| `narwhal_monitoring_core_failures_total`        | Failed monitoring passes during the process lifetime.             | none    |
| `narwhal_monitoring_stage_failures_total`       | Failure count for one monitoring stage.                           | `stage` |
| `narwhal_monitoring_stage_consecutive_failures` | Consecutive failures for one monitoring stage.                    | `stage` |
| `narwhal_event_loop_lag_seconds`                | Delay beyond the latest scheduled monitoring deadline.            | none    |
| `narwhal_event_loop_lag_high_water_seconds`     | Largest monitoring-deadline delay observed by the router process. | none    |

Monitoring stages are `controller`, `health`, `drains`, `rollover`, `readmission`, `liveness`, `handoff`, and `telemetry`. The telemetry stage covers floor-state refresh and loop logging.

### Engine breakers

| Metric                             | Meaning                                                                                   | Labels         |
| ---------------------------------- | ----------------------------------------------------------------------------------------- | -------------- |
| `narwhal_engine_breaker_streak`    | Consecutive failures for an engine and failure class. Zero-valued series remain exported. | `iid`, `class` |
| `narwhal_engine_breaker_verifying` | `1` while an engine verification probe is running.                                        | `iid`, `kind`  |

Failure classes are `connection`, `timeout`, `overload`, `inference_status`, `kv_handoff`, `stream`, and `liveness`.

Verification probes use `verify_health` or `verify_inference`.

When a probe resolves, Narwhal either clears the relevant failure streaks or ejects the engine. Ejection state is reported through `narwhal_ejected`.

`tools/prometheus-alerts.yml` contains the shipped alert expressions. `tools/grafana-narwhal.json` contains the dashboard definition. [Dashboard definitions](https://github.com/athrael-soju/Narwhal/blob/main/tools/observability/README.md) documents panel scope and metric boundaries.

## Contract versions

Versioned Narwhal interfaces declare both a schema name and a schema version. Readers accept documents with the expected schema name and current version. Other schema/version combinations fail validation.

Inspect the contract versions installed with the package:

```bash
narwhal-check --print-contract-versions
```

| Interface            | Schema                      | Version |
| -------------------- | --------------------------- | ------: |
| Native fleet config  | `narwhal.fleet`             |       1 |
| Engine profile store | `narwhal.profiles`          |       1 |
| Engine attestation   | `narwhal.attestation`       |       1 |
| Router handoff       | `narwhal.handoff`           |       1 |
| Router lease         | `narwhal.router-lease`      |       1 |
| Engine lifecycle     | `narwhal.lifecycle`         |       1 |
| Request journal      | `narwhal.journal`           |       1 |
| Live state           | `narwhal.state`             |       1 |
| Prometheus metrics   | `narwhal.metrics`           |       1 |
| Contract manifest    | `narwhal.contract-manifest` |       1 |

Fields documented by these schema versions are compatibility commitments. Incompatible changes require a new schema version. Native Python modules, undocumented fields, log text, and human-readable tables are outside that compatibility contract.

Before upgrading, compare contract manifests. Retain the previous code, configuration, profiles, and compatible state together as the rollback set.