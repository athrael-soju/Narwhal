---
description: Read Narwhal Prometheus metrics for scheduling, role control, latency and demand.
---

# Metrics and role controller state

## Prometheus metrics

For the JSON state document, use [`GET /narwhal/state`](../http-api/05-Live-State.md). For the Grafana panels, follow [Reading the dashboard](../observability/05-Dashboard.md).

A new router process starts every counter and histogram at `0`, and `narwhal_retry_credits` at `serving.retry_budget`. It starts `narwhal_membership_generation` at the handoff's generation on resume and standby takeover when the handoff carries a changed membership, otherwise at `0`.

Each counter counts what that router process observed. On resume and standby takeover, the router adopts the request totals behind these counters from the state handoff: `narwhal_offered_total`, `narwhal_unsized_offered_total`, `narwhal_served_total`, `narwhal_slo_met_total`, `narwhal_expired_total`, `narwhal_invalid_requests_total`, `narwhal_failed_total`, `narwhal_unserved_total`, `narwhal_refused_total`, `narwhal_rejected_total` and `narwhal_cancelled_total`, and the risk-event counts behind `narwhal_demand_evidence_risk_events_total`. [`GET /narwhal/state`](../http-api/05-Live-State.md) and the next handoff include the adopted totals, and `/metrics` leaves them out. Each counter therefore rises only with the process's own requests, and returns to `0` only when the process restarts, which Prometheus treats as a counter reset. To count the requests of a router pair, sum each counter over its routers.

A standby whose most recent probe of its primary found the primary controlling the fleet counts each request it refuses in `narwhal_standby_refusals_total` only. The load balancer retries those attempts on the primary, which counts each request once, so they stay out of `narwhal_offered_total`, `narwhal_unsized_offered_total` and `narwhal_rejected_total`. Every other router counts the requests it refuses as rejected requests, because no live lease holder can take the retry. That covers a standby whose last probe failed, such as while it waits for a killed primary's lease to expire, a router releasing its lease at shutdown, a fenced router, a single router, and a lease holder during a whole-wave hold or with no live engines. A request that reaches the standby and ends another way, after the standby takes over, counts as offered when it ends.

Compare a router's counters with the journal rows of its process's `run`. Rows with `standby_refusal` set match `narwhal_standby_refusals_total`, and the other rows match the outcome counters.

### Metric families

The router exports these series on `/metrics`, grouped by area:

| Area | Series |
| --- | --- |
| Contract identity | `narwhal_contract_info` |
| Admission mode | `narwhal_admission_info`, `narwhal_admission_margin` |
| Router lease | `narwhal_router_ready`, `narwhal_router_lease_epoch`, `narwhal_router_role` |
| Engine monitoring | `narwhal_monitoring_degraded`, `narwhal_monitoring_core_consecutive_failures`, `narwhal_monitoring_core_failures_total`, `narwhal_monitoring_stage_failures_total`, `narwhal_monitoring_stage_consecutive_failures`, `narwhal_event_loop_lag_seconds`, `narwhal_event_loop_lag_high_water_seconds`, `narwhal_event_loop_busy_seconds_total` |
| Request outcomes | `narwhal_offered_total`, `narwhal_unsized_offered_total`, `narwhal_expired_total`, `narwhal_served_total`, `narwhal_slo_met_total`, `narwhal_failed_total`, `narwhal_unserved_total`, `narwhal_refused_total`, `narwhal_rejected_total`, `narwhal_standby_refusals_total`, `narwhal_cancelled_total`, `narwhal_invalid_requests_total` |
| Attempts and quota | `narwhal_prefill_attempts_total`, `narwhal_decode_attempts_total`, `narwhal_retry_attempts_total`, `narwhal_attempt_failures_total`, `narwhal_served_after_retry_total`, `narwhal_retry_credits`, `narwhal_retry_credits_spent_total`, `narwhal_retry_denied_total`, `narwhal_decode_tokens_observed_total`, `narwhal_output_tokens_total`, `narwhal_upstream_seconds_total` |
| Queueing | `narwhal_queued`, `narwhal_queue_capacity`, `narwhal_queue_high_water`, `narwhal_waiting_prefill`, `narwhal_waiting_decode`, `narwhal_queue_wait_seconds` |
| In-flight limit and saturation | `narwhal_admission_inflight`, `narwhal_admission_inflight_limit`, `narwhal_http_retained`, `narwhal_http_retained_limit`, `narwhal_http_retained_high_water`, `narwhal_router_loop_lag_seconds`, `narwhal_request_sizing_delay_seconds`, `narwhal_saturation_threshold_seconds` |
| Pools | `narwhal_pool_instances`, `narwhal_pool_load`, `narwhal_instance_role`, `narwhal_resident_requests`, `narwhal_engine_requests_total`, `narwhal_engine_seats`, `narwhal_kv_lease_seconds`, `narwhal_handoff_bound_seconds` |
| Health | `narwhal_ejected_instances`, `narwhal_ejected`, `narwhal_engine_quarantined`, `narwhal_engine_held`, `narwhal_engine_ejections_total`, `narwhal_engine_hold_starts_total`, `narwhal_engine_hold_ends_total`, `narwhal_engine_probes_total`, `narwhal_engine_readmissions_total`, `narwhal_probation_instances`, `narwhal_health_windows_scored_total`, `narwhal_health_windows_undersampled_total`, `narwhal_health_prefill_paused`, `narwhal_health_prefill_pauses_total`, `narwhal_engine_breaker_streak`, `narwhal_engine_breaker_verifying` |
| Floors | `narwhal_prefill_below_floor`, `narwhal_decode_floor`, `narwhal_decode_below_floor`, `narwhal_prefill_below_floor_events_total`, `narwhal_prefill_below_floor_seconds_total`, `narwhal_decode_floor_restorations_total` |
| Role controller | `narwhal_flips_total`, `narwhal_flip_reversals_total`, `narwhal_flips_refused_total`, `narwhal_flip_inflight_total`, `narwhal_controller_advisory`, `narwhal_controller_decisions_total`, `narwhal_controller_proposed_engines`, `narwhal_controller_phase_work_engines`, `narwhal_controller_projected_slo_ratio`, `narwhal_controller_objective`, `narwhal_controller_decode_tokens_per_engine`, `narwhal_controller_decode_requests_per_engine`, `narwhal_controller_decode_model`, `narwhal_controller_last_decision` |
| Demand history | `narwhal_demand_history_cells`, `narwhal_demand_history_cell_limit`, `narwhal_demand_history_observations`, `narwhal_demand_history_overflow_observations` |
| Attainment evidence | `narwhal_attainment_evidence_covered_seconds`, `narwhal_attainment_evidence_outcomes`, `narwhal_attainment_evidence_buckets`, `narwhal_attainment_evidence_pruned_total` |
| Consolidation | `narwhal_demand_evidence_span_seconds`, `narwhal_demand_evidence_arrivals`, `narwhal_demand_evidence_closed`, `narwhal_demand_evidence_risk_age_seconds`, `narwhal_demand_evidence_short_decode_engines`, `narwhal_demand_evidence_envelope_decode_engines`, `narwhal_demand_evidence_trend_ratio`, `narwhal_demand_evidence_refused`, `narwhal_demand_evidence_risk_events_total` |
| Latency | `narwhal_slo_seconds`, `narwhal_ttft_seconds`, `narwhal_tpot_seconds`, `narwhal_seat_seconds` |
| Lifecycle | `narwhal_engine_draining`, `narwhal_engine_leaving`, `narwhal_engine_ready_to_stop`, `narwhal_engine_lifecycle_state` |
| Membership | `narwhal_membership_generation`, `narwhal_engine_joins_total`, `narwhal_engine_leaves_total` |
| Deployment | `narwhal_deployment_info`, `narwhal_deployment_stage_state`, `narwhal_deployment_updated_timestamp_seconds`, `narwhal_deployment_last_passed_timestamp_seconds`, `narwhal_deployment_qualified_timestamp_seconds`, `narwhal_deployment_profiled_timestamp_seconds`, `narwhal_deployment_calibrated_timestamp_seconds`, `narwhal_fabric_edge_measured_gbps`, `narwhal_fabric_edge_budget_gbps`, `narwhal_engine_attestation_generation_info`, `narwhal_engine_attested_timestamp_seconds` |
| Supervision | `narwhal_supervisor_restarts` |

`narwhal_engine_requests_total` counts the requests the router has sent to each engine since the router started or the engine joined, including its verification, readmission and peer release probes. A joining engine's count starts at 0 when the join adds it to the membership, after its join checks. A join's fabric check counts toward each member it pairs with.

Series with an `iid` label cover the engines in the [membership snapshot](../http-api/08-Engine-Membership.md). When an engine leaves the fleet, its `iid` series stop. When an engine joins, its counters start at 0, including when it rejoins under an `iid` it held before.

### Tool metrics

With `--metrics-url` or `NARWHAL_TOOL_METRICS_URL`, `narwhal-check` serves these gauges on `/metrics` during preflight and first-token calibration, and `narwhal-profile` serves them during a live sweep. Each series carries an `operation` label of `preflight`, `calibration` or `profiling`.

| Series | Value |
| --- | --- |
| `narwhal_tool_operation` | `1` while the operation runs |
| `narwhal_tool_engine_in_use` | `1` for each engine the operation sends requests to, labelled with the engine's `iid` |
| `narwhal_tool_steps_completed` | Steps completed |
| `narwhal_tool_steps_total` | Steps planned |

### Deployment and restart metrics

A router started with `--deployment-status PATH` exports the [deployment status](06-Deployment-Runs.md#deployment-status) in that file. It reads the file on each scrape and exports nothing for a missing or unreadable file.

| Series | Labels | Value |
| --- | --- | --- |
| `narwhal_deployment_info` | `run_id`, `target` | 1 for the run that wrote the status |
| `narwhal_deployment_stage_state` | `stage`, `stage_instance`, `state` | 1 for the state of each stage instance of that run |
| `narwhal_deployment_updated_timestamp_seconds` | | When a stage of that run last ended |
| `narwhal_deployment_last_passed_timestamp_seconds` | | When a run last passed every stage, carried across later runs that failed |
| `narwhal_deployment_qualified_timestamp_seconds`, `narwhal_deployment_profiled_timestamp_seconds`, `narwhal_deployment_calibrated_timestamp_seconds` | | When `preflight`, `profile` and `calibrate` last passed |
| `narwhal_fabric_edge_measured_gbps`, `narwhal_fabric_edge_budget_gbps` | `edge` | The lowest bandwidth fabric qualification measured on the edge, and the highest that one of its KV handoffs needs, in Gbit/s |
| `narwhal_engine_attestation_generation_info` | `iid`, `generation` | 1 for the binding digest of each engine's attestation |
| `narwhal_engine_attested_timestamp_seconds` | `iid` | When the attestation was captured |

Both routers of a pair export the same status, so read these series with `max by` over the routers rather than `sum`. A router that missed the last upload keeps exporting the status before it; keep only the routers whose `narwhal_deployment_updated_timestamp_seconds` is the fleet's newest, as the dashboard does.

`narwhal_supervisor_restarts{process}` counts how often systemd restarted the process since its unit started. The router exports it as `process="router"`, and each attestation sidecar on its own `/metrics` as `process="sidecar"`, when they run under the systemd unit that `narwhal deploy` writes. A sidecar in an engine pod started with `--pod NAMESPACE/NAME` also exports `narwhal_kubelet_restarts{container}`, the kubelet restart count of each container in its pod. Both read their counts at most every 15 seconds.

## Reading outcome reasons

These series split request outcomes and failed attempts by their [journal reason](01-Journal.md#outcome-reasons):

| Metric | Type | Labels | Value |
| --- | --- | --- | --- |
| `narwhal_failed_total` | counter | `reason` | Requests that ended in an error. |
| `narwhal_refused_total` | counter | `cause` | Predictive refusals. |
| `narwhal_rejected_total` | counter | `reason` | Capacity and router-readiness rejections. |
| `narwhal_standby_refusals_total` | counter | `reason` | Requests a standby refused while its last probe found its primary controlling the fleet, with the `narwhal_rejected_total` reasons. |
| `narwhal_expired_total` | counter | `reason` | Expiries. |
| `narwhal_attempt_failures_total` | counter | `phase`, `reason` | Entries added to journal `attempt_failures`, present after the first failure. |

The four outcome counters and `narwhal_standby_refusals_total` export every documented reason other than `unclassified` from process start, at `0` until it occurs. An `unclassified` series appears once the router counts one. Sum a counter over its `reason` or `cause` label for the process's total. `/narwhal/state` reports that total plus the count adopted from a handoff.

## Reading admission mode and retries

| Metric | Type | Labels | Value |
| --- | --- | --- | --- |
| `narwhal_admission_info` | gauge | `mode` | `1` on the series for `serving.admission`. |
| `narwhal_admission_margin` | gauge | | `serving.admission_margin`. |
| `narwhal_served_after_retry_total` | counter | | Completed requests whose final attempt followed a failed one. |

## Reading router lease gauges

| Metric | Type | Labels | Value |
| --- | --- | --- | --- |
| `narwhal_router_ready` | gauge | | `1` while the router admits new traffic, when `/ready` returns HTTP 200. |
| `narwhal_router_lease_epoch` | gauge | | Epoch of the lease this process last claimed or renewed. `0` before the process holds the lease, and on a router without a lease. The epoch is the lease file's `epoch`, or the Kubernetes Lease's `leaseTransitions`. |
| `narwhal_router_role` | gauge | `role` | `1` on the series for the router's role, `0` on the others. |

The `role` label takes these values:

| Role | Router |
| --- | --- |
| `active` | Controls the fleet. A router started without `--lease-path` or `--kubernetes-lease` is always active. |
| `standby` | Shadows the active router, and takes over when the active router releases the lease or the lease expires. |
| `fenced` | Released or lost the lease, holds a lease that has lapsed before its renewal failed, or had its takeover blocked. The `reason` of its [fleet-state refusal](../http-api/04-Inspection.md#fleet-state-reads-on-a-router-without-the-lease) gives the cause, such as `shutting down` or `lease renewal failed`. A standby whose takeover attempt found no fresh handoff, or could not reach the lease, reports `standby` again once the active router answers its probe. |

An active router can report `narwhal_router_ready` `0`, for example with no available engines. The router that claimed the lease most recently reports the highest `narwhal_router_lease_epoch`.

## Reading engine lifecycle gauges

The lifecycle gauges report fields of each engine's record in [`GET /narwhal/lifecycle`](../http-api/07-Handoff-and-Lifecycle.md#get-narwhallifecycle).

| Metric | Type | Labels | Value |
| --- | --- | --- | --- |
| `narwhal_engine_draining` | gauge | `iid` | `1` when `draining` is `true`, otherwise `0`. |
| `narwhal_engine_leaving` | gauge | `iid` | `1` when `leaving` is `true`, otherwise `0`. |
| `narwhal_engine_ready_to_stop` | gauge | `iid` | `1` when `ready_to_stop` is `true`, otherwise `0`. |
| `narwhal_engine_lifecycle_state` | gauge | `iid`, `state` | `1` on the series whose `state` label matches the engine's `state`. |

## Reading membership changes

The membership series report the router's [membership snapshot](../http-api/08-Engine-Membership.md) and the joins and removals the router process handled:

| Metric | Type | Labels | Value |
| --- | --- | --- | --- |
| `narwhal_membership_generation` | gauge | | The snapshot's `generation`, as `GET /narwhal/engines` reports it. |
| `narwhal_engine_joins_total` | counter | `outcome` | Join requests since the router started, by outcome. |
| `narwhal_engine_leaves_total` | counter | `outcome` | Removal requests since the router started, by outcome. |

Each outcome counts these events:

| Series | Counts |
| --- | --- |
| `narwhal_engine_joins_total{outcome="joined"}` | Each `joined` [engine lifecycle event](01-Journal.md#engine-lifecycle-events). |
| `narwhal_engine_joins_total{outcome="refused"}` | Each `join_refused` event. |
| `narwhal_engine_leaves_total{outcome="left"}` | Each `left` event. |
| `narwhal_engine_leaves_total{outcome="refused"}` | Each [`DELETE /narwhal/engines/{iid}`](../http-api/08-Engine-Membership.md#delete-narwhalenginesiid) answered with HTTP `409`: a removal rule refused it, or `deadline_s` is zero, negative or infinite. |
| `narwhal_engine_leaves_total{outcome="cancelled"}` | Each leave that ended before the engine left, when one of these replaced the engine's drain: a readmission of the engine, a whole-wave restart hold, or a whole-wave drain that took over the held engines while no engine was schedulable. |

Both counters export every outcome from process start, at `0` until it occurs. A standby reports the generation of its own snapshot until it takes over. A resumed router and a standby that takes over adopt the handoff's generation, and start both counters at `0`.

## Inspecting scheduling and role control

Pools and resident work:

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `narwhal_instance_role` | gauge | `iid`, `role` | `1` on the series for each engine's current role. |
| `narwhal_pool_instances` | gauge | `role` | Engines in the pool. |
| `narwhal_pool_load` | gauge | `role` | Pool load normalized per phase in [Role control](../configuration/02-Serving-and-Role-Control.md#role-control), with `1.0` at the phase target. |
| `narwhal_resident_requests` | gauge | `iid`, `phase` | Requests resident on the engine. |
| `narwhal_engine_seats` | gauge | `iid`, `phase` | The engine's [seats](../configuration/02-Serving-and-Role-Control.md#engine-seats), `0` for no limit. |

Role changes:

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `narwhal_flips_total` | counter | `to`, `by` | Role changes by target pool and caller. The series for these `by` and `to` pairs start at `0`: `decode_floor` and `decode`, `floor_recovery` and `prefill`, `reactive` and `decode`, and `reactive` and `prefill`. |
| `narwhal_flip_reversals_total` | counter | | Moves whose target role differs from the same engine's previous recorded move, counted from that engine's second move. |
| `narwhal_flips_refused_total` | counter | | Role changes blocked by timing, availability, role pins, role floors, the resident guard, shared-device profile coverage, or advisory mode. |
| `narwhal_flip_inflight_total` | counter | `phase` | Requests resident on an engine at the moment it changed role. |

Controller decisions:

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `narwhal_controller_advisory` | gauge | | `1` while the controller records decisions without changing roles. |
| `narwhal_controller_decisions_total` | counter | `by`, `result` | Controller decisions by caller and result. |

These series appear after the controller records its first decision:

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `narwhal_controller_last_decision` | gauge | `by`, `reason`, `result` | `1` on the most recent decision. |
| `narwhal_controller_proposed_engines` | gauge | `role` | Engines per pool in the most recent decision. |
| `narwhal_controller_projected_slo_ratio` | gauge | `phase` | Projected SLO ratio after the most recent proposal. |
| `narwhal_controller_phase_work_engines` | gauge | `phase` | Measured phase work in engine equivalents. |
| `narwhal_controller_objective` | gauge | `value`: `candidate`, `improvement` | Candidate pressure, and its improvement over the current split. |
| `narwhal_controller_decode_tokens_per_engine` | gauge | `value`: `projected`, `slo_capacity`, `kv_capacity` | Projected decode tokens per engine, beside the profiled SLO and KV capacity. |
| `narwhal_controller_decode_requests_per_engine` | gauge | | Projected active decode requests per engine. |
| `narwhal_controller_decode_model` | gauge | `value`: `correction`, `profile_covered` | Live decode correction, and `1` when the measured profile domain covers the decision. |

## Reading latency histograms

`narwhal_slo_seconds` exports the configured `ttft` and `tpot` budgets through the `metric` label. Its `slo` label, and the `slo` label on each latency histogram, carry the target that scaled the histogram's bucket edges.

| Histogram | Measures | Bucket scale |
| --- | --- | --- |
| `narwhal_ttft_seconds` | Arrival to prefill completion. | `slo.ttft_s` |
| `narwhal_tpot_seconds` | Mean seconds per output token after prefill completion. | `slo.tpot_s` |
| `narwhal_queue_wait_seconds` | Wait per original request at each `stage` it reached: `admission`, `prefill` seat, or `decode` seat. | `serving.queue_timeout_s`, or `slo.ttft_s` when it is `0` |
| `narwhal_seat_seconds` | Time a request holds an admission seat. | `slo.ttft_s` |

Bucket boundaries, as multiples of the bucket scale:

```text
0.025, 0.05, 0.1, 0.2, 0.35, 0.5, 0.7, 1.0, 1.5, 3.0, 10.0, +Inf
```

Histogram aggregation:

- Compute quantiles from bucket rates grouped by `instance`, `slo` and `le`, and by `stage` for `narwhal_queue_wait_seconds`.
- Divide a `narwhal_ttft_seconds` or `narwhal_tpot_seconds` quantile by the `narwhal_slo_seconds` series with the matching `metric`, `instance` and `slo`.

## Inspecting retained attainment evidence

`narwhal_attainment_evidence_pruned_total` appears with a `kind` label of `buckets` or `outcomes` after the first buckets age out of the [attainment retention window](../http-api/06-SLO-and-Demand.md#slo-attainment).

## Inspecting demand history and floors

Demand histories clear on router restart.

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `narwhal_demand_history_cells` | gauge | `window`: `unsized`, `arrivals`, `expected_decode`, `observed_decode`, `residency` | Retained demand cohorts. |
| `narwhal_demand_history_cell_limit` | gauge | same `window` values | Maximum retained cohorts. |
| `narwhal_demand_history_observations` | gauge | same `window` values | Original observations represented by the window. |
| `narwhal_demand_history_overflow_observations` | gauge | same `window` values | Observations merged past the cohort limit. |

The floor series report the live pools against `min_prefill` and `min_decode`:

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `narwhal_decode_floor` | gauge | | Configured `min_decode`. |
| `narwhal_decode_below_floor` | gauge | | `1` while live decode capacity is below `min_decode`. |
| `narwhal_decode_floor_restorations_total` | counter | | Moves made to restore the decode floor. |
| `narwhal_prefill_below_floor` | gauge | | `1` while the live prefill pool is below `min_prefill`. |
| `narwhal_prefill_below_floor_events_total` | counter | | Prefill floor breaches. |
| `narwhal_prefill_below_floor_seconds_total` | counter | | Seconds spent below the prefill floor. |

## Inspecting consolidation gates

These metrics expose the consolidation evidence that gates decode-to-prefill moves:

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `narwhal_demand_evidence_span_seconds` | gauge | | Duration of the retained arrival window in seconds. |
| `narwhal_demand_evidence_arrivals` | gauge | | Arrival samples in the retained arrival window. |
| `narwhal_demand_evidence_closed` | gauge | | `1` after the evidence window closes. |
| `narwhal_demand_evidence_risk_age_seconds` | gauge | | Age of the newest risk event in seconds. |
| `narwhal_demand_evidence_short_decode_engines` | gauge | | Short-horizon decode demand in engine equivalents. |
| `narwhal_demand_evidence_envelope_decode_engines` | gauge | | Conservative decode-demand envelope in engine equivalents. |
| `narwhal_demand_evidence_trend_ratio` | gauge | | Short-horizon demand over long-horizon demand, present once a long-horizon estimate exists. |
| `narwhal_demand_evidence_refused` | gauge | `gate`: `risk`, `evidence`, `trend` | `1` when the gate blocks consolidation. |
| `narwhal_demand_evidence_risk_events_total` | counter | `kind`: `first_token_timeout`, `p_to_d_recovery` | Risk-event count, present after this process records its first risk event. |
