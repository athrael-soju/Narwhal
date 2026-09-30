# How Narwhal tracks SLOs and demand

## SLO attainment

The `attainment` object has these fields:

```text
bucket_s
retained_s
covered_s
buckets
outcomes
pruned_buckets
pruned_outcomes
```

Completed, failed, expired, and predictively refused requests are grouped into buckets `monitor_interval_s` wide. Each bucket counts how many requests met TTFT, how many met TPOT, and how many there were in total.

Narwhal keeps four demand windows' worth of buckets, measured back from the newest one, and a window query includes the whole bucket that sits on its boundary. `covered_s` is the age of the oldest bucket still kept, capped at the retention span.

## Demand accounting

### Unsized offers

`demand_history.unsized` tracks requests that Narwhal counted as arrivals but never sized:

- `pending` counts requests whose bodies are still being read.
- `observations` counts requests that ended before their body was sized. Invalid bodies aren't counted.

It also reports the same window fields as the other `demand_history` entries: `bucket_s`, `retained_s`, `cells`, `cell_limit`, and `overflow_observations`.

While `pending` is nonzero or any unsized request falls inside the demand window, Narwhal marks demand incomplete.

### Input-size repricing

A parsed request enters demand history with a local estimate of its input size. If tokenization finishes after admission, the exact count replaces the estimate at the original arrival time. Requests rejected before tokenization keep the estimate.

Repricing can move a request into a different cohort. If that request was the last observation defining a bucket boundary, the evidence for that boundary is thrown out.

### Bucketing and retention

A demand bucket is as wide as the smallest of one second, the controller step, and the minimum evidence span. Each bucket holds up to 128 exact request shapes plus one overflow cohort. Unsized offers are stored as a single shape per bucket.

Arrival and residency data are kept for one demand window, and completed output observations for four. Old buckets are pruned whenever new evidence is recorded, so they still expire while the control loop is stopped.

### Overflow

The overflow cohort keeps every request count, but it prices the whole cohort using its largest input length and requested output length. That simplification has a few knock-on effects:

- If the overflow cohort's requested output length is zero, decode demand is marked incomplete.
- Overflow in output history switches off learned discounts until the affected observations expire.
- A cohort that straddles a window boundary counts in full, which can stretch history by up to one bucket.

`demand_history` and the `narwhal_demand_history_*` metrics show retained cells, the cell limit, counted observations, and overflow observations.

### Decision snapshots

Each reactive controller decision snapshots everything it needs to score candidate splits: profile coefficients, offered-work demand, observed phase pressure, pending output estimates, and resident work still in the old role. Profiles are frozen and split inputs are copied by value, so a score stays fixed once it's computed even if live state keeps changing.

Output-length estimates and decode correction are computed once per decision and shared by both demand horizons.

### Prefill recovery ratio

When `queued_prefill_s > 0` and at least one engine is in the prefill role, Narwhal uses:

```text
recovery_prefill_ratio = max(
  observed prefill pressure,
  (resident prefill seconds + queued prefill seconds)
    / (current prefill engine count * TTFT SLO seconds)
)
```

Otherwise it uses observed prefill pressure alone.

Decisions made on incomplete demand show `recovery_prefill_ratio`, `queued_prefill_s`, and both observed phase ratios. Their `decision_basis` is `prefill_pressure_recovery` or `decode_pressure_recovery`.

Source-pressure and movement-gate checks use demand at full precision. The values are rounded only when written to state and journal records, so a displayed number can differ slightly from the one the gate actually compared.

Every move also has to clear role floors, live availability, cooldown, dwell, profile coverage, and physical KV limits. [Role control](../configuration/02-Serving-and-Role-Control.md#7-role-control) covers the movement and confirmation gates.

### Consolidation evidence

Consolidation evidence only counts observations whose timestamps are known to fall after its cutoff. `demand_evidence` exposes:

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

It is captured for every decode-to-prefill gate, including decisions blocked before any engine moved.
