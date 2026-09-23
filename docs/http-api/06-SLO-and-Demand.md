# SLO attainment and demand accounting

## SLO attainment

The `attainment` object contains:

```text
bucket_s
retained_s
covered_s
buckets
outcomes
pruned_buckets
pruned_outcomes
```

Narwhal buckets completed, failed, expired, and predictively refused requests by `monitor_interval_s`, recording TTFT-met, TPOT-met, and total counts for each bucket. Pruning advances from the newest recorded bucket, retains four demand windows, and includes the full boundary bucket in window queries.

`covered_s` reports the age of the oldest retained bucket, capped at the configured retention span.

---

## Demand accounting

### Unsized offers

Inside:

```text
demand_history.unsized
```

`pending` counts request bodies still being read.

`observations` counts retained offers that terminated before workload sizing.

### Input-size repricing

Parsed offers enter demand history at a local input-size estimate. When tokenization finishes after admission, Narwhal replaces the estimate at the original arrival timestamp; requests rejected before tokenization keep the local estimate. If repricing moves the last observation defining a bucket boundary into another cohort, Narwhal invalidates that boundary evidence.

### Bucketing and retention

Each demand bucket stores:

- at most 128 exact request shapes
- one overflow cohort

Narwhal stores unsized offers as one shape per time bucket.

Bucket width is the minimum of:

- one second
- controller step
- minimum evidence span

Retention differs by evidence type:

- arrival and residency data: one demand window
- completed output observations: four demand windows

Recording new evidence prunes expired buckets even when the control loop is stopped.

### Overflow

Overflow retains every request count and prices the cohort from its largest input length and requested output length.

An overflow cohort with a zero requested output length marks decode demand incomplete.

Overflow in output history disables learned discounts until the affected observations expire.

A cohort crossing a window boundary contributes its complete count, adding at most one bucket of history.

Consolidation evidence counts observations whose timestamps are known to fall after its cutoff.

`demand_history` and the `narwhal_demand_history_*` metrics expose:

- retained cells
- cell limit
- counted observations
- overflow observations

### Decision snapshots

Every reactive controller decision snapshots the inputs required to score candidate splits:

- profile coefficients
- offered-work demand
- observed phase pressure
- pending output estimates
- resident work in the old role

Frozen profiles and value-only split inputs prevent later live-state changes from modifying an already computed score.

Output-length estimates and decode correction are built once and reused across both demand horizons.

### Prefill recovery ratio

With priced prefill waiters, Narwhal sets `recovery_prefill_ratio` to the larger of observed prefill pressure and resident-plus-queued prefill seconds divided by the current prefill engine count and TTFT SLO. For zero priced waiters, it uses observed prefill pressure.

Incomplete-demand decisions expose:

```text
recovery_prefill_ratio
queued_prefill_s
```

along with both observed phase ratios.

Their `decision_basis` is one of:

```text
prefill_pressure_recovery
decode_pressure_recovery
```

See [Role control](../configuration/02-Serving-and-Role-Control.md#7-role-control) for movement and confirmation gates.

The controller carries full-precision demand into source-pressure and movement-gate checks, then rounds the corresponding values when it writes state and journal records.

Every move is constrained by:

- role floors
- live availability
- cooldown
- dwell
- profile coverage
- physical KV limits

### Consolidation evidence

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
