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

It stores diagnostic outcomes for requests that are:

- completed
- failed
- expired
- predictively refused

Requests contribute:

- TTFT-met counts
- TPOT-met counts
- total counts

Bucket width is `monitor_interval_s`.

Pruning:

- is anchored to the newest recorded bucket
- retains four demand windows
- includes the full boundary bucket in window queries

`attainment` reports retained and already-pruned counts.

`covered_s` reports the age of the oldest retained bucket, capped at the configured retention span.

---

## Demand accounting

`demand_history` records the request stream used by the reactive controller.

### Unsized offers

Inside:

```text
demand_history.unsized
```

`pending` counts request bodies still being read.

`observations` counts retained offers that terminated before workload sizing.

Demand history includes authenticated offers that passed request validation.

### Input-size repricing

Parsed offers initially enter demand history using local input-size estimates.

If the request later reaches admission and tokenization completes, the tokenizer result replaces the estimate at the request's original arrival timestamp.

Requests rejected before tokenization retain the local estimate.

Repricing preserves bounded shape aggregation.

If the last observation defining a bucket boundary moves into another cohort, that boundary evidence becomes invalid.

### Bucketing and retention

Each demand bucket stores:

- at most 128 exact request shapes
- one overflow cohort

Unsized history needs only one shape.

Bucket width is the minimum of:

- one second
- controller step
- minimum evidence span

Retention differs by evidence type:

- arrival and residency data: one demand window
- completed output observations: four demand windows

Recording new evidence prunes expired buckets even when the control loop is stopped.

### Overflow

Overflow preserves every request count.

When pricing work, it uses the largest:

- input length
- requested output length

within the overflow cohort.

Uncapped overflow represents incomplete demand.

Overflow in output history disables learned discounts until the affected observations expire.

A cohort crossing a window boundary contributes its complete count, adding at most one bucket of history.

Consolidation evidence includes only observations guaranteed to occur after its cutoff.

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

When priced prefill waiters exist:

```text
recovery_prefill_ratio
```

is the greater of:

1. observed prefill pressure
2. resident-plus-queued prefill seconds divided by current prefill engine count and the TTFT SLO

With no priced prefill waiters, observed pressure is used directly.

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

Consolidation checks retain full-precision demand through:

- source-pressure evaluation
- movement gating

Rounded state and journal values are presentation values only.

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

The top-level record represents consolidation evidence for every decode-to-prefill gate, including:

- retained span
- sample count
- required minima
- bounded lookback
- closure state
- short-horizon decode estimate
- long-horizon decode estimate
- trend ratio
- conservative envelope
- active risk event
- per-kind risk counts
