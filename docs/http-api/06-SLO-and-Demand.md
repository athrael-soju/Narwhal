# SLO attainment and demand accounting

## SLO attainment

`attainment` reports SLO results per time bucket:

```text
bucket_s
retained_s
covered_s
buckets
outcomes
pruned_buckets
pruned_outcomes
```

Narwhal groups completed, failed, expired, and predictively refused requests into buckets of `monitor_interval_s`. Each bucket counts TTFT-met, TPOT-met, and total requests.

Pruning starts from the newest bucket and keeps four demand windows. Window queries include the whole boundary bucket.

`covered_s` reports the age of the oldest retained bucket, capped at the configured retention span.

---

## Demand accounting

Demand history records the work offered to the role controller and the output it observed. One demand window is `controller.reactive.window_s` seconds long.

### Unsized offers

`demand_history.unsized` reports two counts:

- `pending`: request bodies still being read.
- `observations`: retained offers that terminated before workload sizing.

### Input-size repricing

Parsed offers enter demand history at a local input-size estimate. When tokenization finishes after admission, Narwhal replaces the estimate at the original arrival timestamp. Requests rejected before tokenization keep the local estimate. Repricing that removes the last observation defining a bucket boundary invalidates that boundary evidence.

### Bucketing and retention

Each demand bucket stores at most 128 exact request shapes and one overflow cohort. Narwhal stores unsized offers as one shape per time bucket.

Bucket width is the smallest of one second, the controller step, and the minimum evidence span. Arrival and residency data stay for one demand window. Completed output observations stay for four.

Recording evidence prunes expired buckets whether or not the control loop runs.

`demand_history` and the `narwhal_demand_history_*` metrics expose the retained cells, cell limit, counted observations, and overflow observations.

### Overflow

Overflow retains every request count and prices the cohort from its largest input length and requested output length.

An overflow cohort whose requested output length is zero marks decode demand incomplete. Overflow in output history disables learned discounts until the affected observations expire. A cohort crossing a window boundary contributes its full count, which can add up to one bucket of history.

### Decision snapshots

Each reactive decision snapshots what it needs to score candidate splits: profile coefficients, demand, phase pressure, output estimates, and resident work. Scores use frozen profiles and copied values, so live state cannot change a score after it is computed.

Output-length estimates and decode correction are built once and reused across both demand horizons.

### Prefill recovery ratio

When `queued_prefill_s > 0` and at least one engine has the prefill role, Narwhal calculates:

```text
recovery_prefill_ratio = max(
  observed prefill pressure,
  (resident prefill seconds + queued prefill seconds)
    / (current prefill engine count * TTFT SLO seconds)
)
```

Without queued prefill, the ratio is the observed prefill pressure.

Incomplete-demand decisions expose both observed phase ratios, `recovery_prefill_ratio`, and `queued_prefill_s`.

The `decision_basis` of these decisions is one of:

```text
prefill_pressure_recovery
decode_pressure_recovery
```

Source-pressure and movement-gate checks use full-precision demand. State and journal records store rounded values.

Role floors, cooldown, dwell, and KV limits all constrain a move. For movement and confirmation gates, see [Role control](../configuration/02-Serving-and-Role-Control.md#7-role-control).

### Consolidation evidence

Consolidation evidence counts only observations timestamped after the cutoff.

`demand_evidence` exposes:

```text
span_s
arrivals
required_span_s
required_arrivals
max_span_s
closed
risk_kind
risk_age_s
risk_events
short_decode_engines
long_decode_engines
trend_ratio
envelope_decode_engines
blocked_gate
```

Narwhal captures `demand_evidence` for every decode-to-prefill gate, including decisions blocked before movement.
