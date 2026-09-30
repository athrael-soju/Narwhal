# Metrics and role controller state

## Read live state from Prometheus

Request-outcome counters restored on resume and standby takeover:

```text
narwhal_served_total
narwhal_failed_total
narwhal_unserved_total
narwhal_refused_total
narwhal_rejected_total
narwhal_cancelled_total
```

Retry quota, histograms, and every other counter start at zero in a new router process.

Split journal rows by `run` when comparing restored outcome counts with offered counts.

### Metric families

| Area                | Series                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Contract identity   | `narwhal_contract_info`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| Router lease        | `narwhal_router_ready`, `narwhal_router_lease_epoch`                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| Engine monitoring   | `narwhal_monitoring_degraded`, `narwhal_monitoring_core_consecutive_failures`, `narwhal_monitoring_core_failures_total`, `narwhal_monitoring_stage_failures_total`, `narwhal_monitoring_stage_consecutive_failures`, `narwhal_event_loop_lag_seconds`, `narwhal_event_loop_lag_high_water_seconds`                                                                                                                                                                                                                        |
| Request outcomes    | `narwhal_offered_total`, `narwhal_unsized_offered_total`, `narwhal_expired_total`, `narwhal_served_total`, `narwhal_failed_total`, `narwhal_unserved_total`, `narwhal_refused_total`, `narwhal_rejected_total`, `narwhal_cancelled_total`, `narwhal_invalid_requests_total`                                                                                                                                                                                                                                               |
| Attempts and quota  | `narwhal_prefill_attempts_total`, `narwhal_decode_attempts_total`, `narwhal_retry_attempts_total`, `narwhal_retry_credits`, `narwhal_retry_credits_spent_total`, `narwhal_retry_denied_total`, `narwhal_decode_tokens_observed_total`, `narwhal_upstream_seconds_total`                                                                                                                                                                                                                                                   |
| Queueing            | `narwhal_queued`, `narwhal_queue_capacity`, `narwhal_queue_high_water`, `narwhal_waiting_prefill`, `narwhal_waiting_decode`, `narwhal_queue_wait_seconds`                                                                                                                                                                                                                                                                                                                                                                 |
| HTTP retention      | `narwhal_http_retained`, `narwhal_http_retained_limit`, `narwhal_http_retained_high_water`                                                                                                                                                                                                                                                                                                                                                                                                                                |
| Pools               | `narwhal_pool_instances`, `narwhal_pool_load`, `narwhal_instance_role`, `narwhal_resident_requests`                                                                                                                                                                                                                                                                                                                                                                                                                       |
| Health              | `narwhal_ejected_instances`, `narwhal_ejected`, `narwhal_probation_instances`, `narwhal_health_windows_scored_total`, `narwhal_health_windows_undersampled_total`, `narwhal_health_prefill_paused`, `narwhal_health_prefill_pauses_total`, `narwhal_engine_breaker_streak`, `narwhal_engine_breaker_verifying`                                                                                                                                                                                                            |
| Floors              | `narwhal_prefill_below_floor`, `narwhal_decode_floor`, `narwhal_decode_below_floor`, `narwhal_prefill_below_floor_events_total`, `narwhal_prefill_below_floor_seconds_total`, `narwhal_decode_floor_restorations_total`                                                                                                                                                                                                                                                                                                   |
| Role controller     | `narwhal_flips_total`, `narwhal_flip_reversals_total`, `narwhal_flips_refused_total`, `narwhal_flip_inflight_total`, `narwhal_controller_advisory`, `narwhal_controller_decisions_total`, `narwhal_controller_proposed_engines`, `narwhal_controller_phase_work_engines`, `narwhal_controller_projected_slo_ratio`, `narwhal_controller_objective`, `narwhal_controller_decode_tokens_per_engine`, `narwhal_controller_decode_requests_per_engine`, `narwhal_controller_decode_model`, `narwhal_controller_last_decision` |
| Demand history      | `narwhal_demand_history_cells`, `narwhal_demand_history_cell_limit`, `narwhal_demand_history_observations`, `narwhal_demand_history_overflow_observations`                                                                                                                                                                                                                                                                                                                                                                |
| Attainment evidence | `narwhal_attainment_evidence_covered_seconds`, `narwhal_attainment_evidence_outcomes`, `narwhal_attainment_evidence_buckets`, `narwhal_attainment_evidence_pruned_total`                                                                                                                                                                                                                                                                                                                                                  |
| Consolidation         | `narwhal_demand_evidence_span_seconds`, `narwhal_demand_evidence_arrivals`, `narwhal_demand_evidence_closed`, `narwhal_demand_evidence_risk_age_seconds`, `narwhal_demand_evidence_short_decode_engines`, `narwhal_demand_evidence_envelope_decode_engines`, `narwhal_demand_evidence_trend_ratio`, `narwhal_demand_evidence_refused`, `narwhal_demand_evidence_risk_events_total` |
| Latency             | `narwhal_slo_seconds`, `narwhal_ttft_seconds`, `narwhal_tpot_seconds`, `narwhal_seat_seconds`                                                                                                                                                                                                                                                                                                                                                                                                                             |
| Lifecycle           | `narwhal_engine_draining`, `narwhal_engine_ready_to_stop`                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |

## Inspect scheduling and role control

| Metric                         | Meaning                                                                                                                         |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------- |
| `narwhal_flip_reversals_total` | Moves whose target role differs from the same engine's previous recorded move, counted from that engine's second move. |
| `narwhal_flips_refused_total`  | Role changes blocked by timing, availability, role pins, role floors, the resident guard, or advisory mode.                     |
| `narwhal_pool_load`            | Pool load normalized per phase in [Role control](../configuration/02-Serving-and-Role-Control.md#7-role-control), with `1.0` at the phase target. |

## Read latency histograms

`narwhal_slo_seconds` exports the configured `ttft` and `tpot` budgets through the `metric` label.

Bucket boundaries for `narwhal_ttft_seconds` and `narwhal_tpot_seconds`, as multiples of the corresponding SLO budget:

```text
0.025, 0.05, 0.1, 0.2, 0.35, 0.5, 0.7, 1.0, 1.5, 3.0, 10.0, +Inf
```

Histograms with configured request-lifecycle bounds:

- `narwhal_queue_wait_seconds`
- `narwhal_seat_seconds` (the time a request holds an admission seat)

Histogram aggregation:

- Compute quantiles from bucket rates grouped by `instance` and `le`.
- Sum buckets only across routers with identical bucket edges.

## Inspect retained attainment evidence

`narwhal_attainment_evidence_pruned_total` is exported with a `kind` label of `buckets` or `outcomes` once the first buckets age out of the [attainment retention window](../http-api/06-SLO-and-Demand.md#slo-attainment).

## Inspect demand history and decode floor

Router restart:

- Demand histories clear.
- `narwhal_decode_floor` resets to `min_decode` from the fleet configuration.

| Metric                                         | Type  | Labels                                                                             | Meaning                                                    |
| ---------------------------------------------- | ----- | ---------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| `narwhal_decode_floor`                         | gauge |                                                                                    | Configured `min_decode`.                                   |
| `narwhal_demand_history_cells`                 | gauge | `window`: `unsized`, `arrivals`, `expected_decode`, `observed_decode`, `residency` | Retained demand cohorts.                                   |
| `narwhal_demand_history_cell_limit`            | gauge | same `window` values                                                               | Maximum retained cohorts.                                  |
| `narwhal_demand_history_observations`          | gauge | same `window` values                                                               | Original observations represented by the window.           |
| `narwhal_demand_history_overflow_observations` | gauge | same `window` values                                                               | Observations merged past the cohort limit. |

## Inspect consolidation gates

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `narwhal_demand_evidence_span_seconds` | gauge |      | Duration of the retained arrival window in seconds. |
| `narwhal_demand_evidence_arrivals` | gauge |      | Arrival samples in the retained arrival window. |
| `narwhal_demand_evidence_closed` | gauge |      | `1` after the evidence window closes. |
| `narwhal_demand_evidence_risk_age_seconds` | gauge |      | Age of the newest risk event in seconds. |
| `narwhal_demand_evidence_short_decode_engines` | gauge |      | Short-horizon decode demand in engine equivalents. |
| `narwhal_demand_evidence_envelope_decode_engines` | gauge |      | Conservative decode-demand envelope in engine equivalents. |
| `narwhal_demand_evidence_trend_ratio` | gauge |      | Short-horizon demand over long-horizon demand, exported once a long-horizon estimate exists. |
| `narwhal_demand_evidence_refused` | gauge | `gate`: `risk`, `evidence`, `trend` | `1` when the gate blocks consolidation. |
| `narwhal_demand_evidence_risk_events_total` | counter | `kind` | Risk-event count. |
