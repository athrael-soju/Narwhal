# Metrics and controller state

## Read live state from Prometheus

Narwhal restores these request-outcome counters on resume and standby takeover:

```text
narwhal_served_total
narwhal_failed_total
narwhal_unserved_total
narwhal_refused_total
narwhal_rejected_total
narwhal_cancelled_total
```

A replacement router process initializes these counters and measurements afresh:

- offered, unsized, and expired counters;
- attempt counters;
- retry quota;
- histograms;
- controller decision counters;
- role-change counters;
- floor counters;
- invalid-request counter.

Use journal `run` as the process boundary when reconciling restored outcome counters with process-local offered counts.

### Metric families

| Area                | Series                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
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

## Inspect scheduling and role control

Role-change counters begin accumulating when the scheduler starts.

`narwhal_flip_reversals_total` increments when an engine moves back from its previous target role, starting with its second recorded move.

`narwhal_flips_refused_total` records role changes blocked by:

- timing;
- availability;
- role pins;
- role floors;
- the resident guard;
- advisory mode.

`narwhal_pool_load` uses the phase-specific normalization documented under [Role control](../configuration/02-Serving-and-Role-Control.md#7-role-control). A value of `1.0` means the phase target has been reached.

## Read latency histograms

`narwhal_slo_seconds` exports the configured `ttft` and `tpot` budgets through the `metric` label.

TTFT and TPOT histograms derive their bucket boundaries from these multiples of the corresponding SLO budget:

```text
0.025
0.05
0.1
0.2
0.35
0.5
0.7
1.0
1.5
3.0
10.0
+Inf
```

Queue-wait and seat-time histograms use the request-lifecycle bounds configured for those measurements.

Compute histogram quantiles from bucket rates grouped by `instance` and `le`.

Aggregate router histograms with identical bucket edges.

## Inspect retained attainment evidence

`narwhal_attainment_evidence_pruned_total` begins appearing after Narwhal has dropped evidence older than every consumer horizon. Its `kind` label identifies the pruned data as either:

```text
buckets
outcomes
```

## Inspect demand history and decode floor

Each Prometheus scrape exports:

- the configured decode floor;
- one sample for every retained demand-history window.

Router restart clears these process-local histories. The new process reads `narwhal_decode_floor` from its fleet configuration.

| Metric                                         | Type  | Labels                                                                             | Value                                                      |
| ---------------------------------------------- | ----- | ---------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| `narwhal_decode_floor`                         | gauge | none                                                                               | Configured `min_decode`.                                   |
| `narwhal_demand_history_cells`                 | gauge | `window`: `unsized`, `arrivals`, `expected_decode`, `observed_decode`, `residency` | Retained demand cohorts.                                   |
| `narwhal_demand_history_cell_limit`            | gauge | same `window` values                                                               | Maximum retained cohorts.                                  |
| `narwhal_demand_history_observations`          | gauge | same `window` values                                                               | Original observations represented by the window.           |
| `narwhal_demand_history_overflow_observations` | gauge | same `window` values                                                               | Observations coalesced after the cohort limit was reached. |

## Inspect consolidation gates

| Metric                                            | Meaning                                                                                                 | Labels                              |
| ------------------------------------------------- | ------------------------------------------------------------------------------------------------------- | ----------------------------------- |
| `narwhal_demand_evidence_span_seconds`            | Duration of the retained arrival window in seconds.                                                     | none                                |
| `narwhal_demand_evidence_arrivals`                | Arrival samples contained in that window.                                                               | none                                |
| `narwhal_demand_evidence_closed`                  | `1` after the evidence window closes.                                                                   | none                                |
| `narwhal_demand_evidence_risk_age_seconds`        | Age of the newest risk event in seconds.                                                                | none                                |
| `narwhal_demand_evidence_short_decode_engines`    | Short-horizon decode demand in engine equivalents.                                                      | none                                |
| `narwhal_demand_evidence_envelope_decode_engines` | Conservative decode-demand envelope in engine equivalents.                                              | none                                |
| `narwhal_demand_evidence_trend_ratio`             | Short-horizon demand divided by long-horizon demand. Exported only when a long-horizon estimate exists. | none                                |
| `narwhal_demand_evidence_refused`                 | `1` for a gate currently blocking consolidation. Every gate is `0` when consolidation is permitted.     | `gate`: `risk`, `evidence`, `trend` |
| `narwhal_demand_evidence_risk_events_total`       | Risk-event count. Appears after the first event.                                                        | `kind`                              |
