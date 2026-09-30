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

| Property          | Value                                                    |
| ----------------- | -------------------------------------------------------- |
| Bucket width      | `monitor_interval_s`                                     |
| Counted requests  | Completed, failed, expired, and predictively refused     |
| Counts per bucket | TTFT-met, TPOT-met, and total requests                   |
| Retention         | Four demand windows, counted back from the newest bucket |
| Window queries    | Include the whole boundary bucket                        |

`covered_s` is the age of the oldest retained bucket, capped at the retention span.

---

## Demand accounting

One demand window is `controller.reactive.window_s` seconds long.

### Unsized offers

`demand_history.unsized` reports two counts:

- `pending`: request bodies being read.
- `observations`: retained offers that terminated before workload sizing.

### Input-size repricing

| Offer                        | Input size in demand history                                     |
| ---------------------------- | ---------------------------------------------------------------- |
| Parsed, at entry             | Local input-size estimate                                        |
| Tokenized after admission    | Tokenization result, recorded at the original arrival timestamp  |
| Rejected before tokenization | Local input-size estimate                                        |

### Bucketing and retention

| Property                            | Value                                                                       |
| ----------------------------------- | --------------------------------------------------------------------------- |
| Bucket width                        | Smallest of one second, the controller step, and the minimum evidence span |
| Exact request shapes                | Up to 128 per bucket                                                        |
| Overflow cohort                     | One per bucket                                                              |
| Unsized offers                      | One shape per time bucket                                                   |
| Arrival and residency data          | Kept for one demand window                                                  |
| Completed output observations       | Kept for four demand windows                                                |
| Pruning                             | On each evidence write                                                      |

`demand_history` and the `narwhal_demand_history_*` metrics expose these quantities:

- retained cells
- cell limit
- counted observations
- overflow observations

### Overflow

An overflow cohort:

- keeps every request count
- is priced from its largest input length and requested output length
- marks decode demand incomplete when its requested output length is zero
- disables learned discounts in output history until the affected observations expire
- contributes its full count when it crosses a window boundary

### Decision snapshots

Reactive decision snapshot contents:

- profile coefficients
- demand
- phase pressure
- output estimates
- resident work

### Prefill recovery ratio

Prefill recovery ratio when `queued_prefill_s > 0` and at least one engine has the prefill role:

```text
recovery_prefill_ratio = max(
  observed prefill pressure,
  (resident prefill seconds + queued prefill seconds)
    / (current prefill engine count * TTFT SLO seconds)
)
```

In any other state, `recovery_prefill_ratio` equals the observed prefill pressure.

Incomplete-demand decisions expose:

- both observed phase ratios
- `recovery_prefill_ratio`
- `queued_prefill_s`

`decision_basis` in incomplete-demand decisions is one of:

```text
prefill_pressure_recovery
decode_pressure_recovery
```

Demand precision:

| Consumer                                 | Precision      |
| ---------------------------------------- | -------------- |
| Source-pressure and movement-gate checks | Full precision |
| State and journal records                | Rounded        |

Role floors, cooldown, dwell, KV limits, and the [movement and confirmation gates](../configuration/02-Serving-and-Role-Control.md#7-role-control) constrain each move.

### Consolidation evidence

`arrivals` counts arrivals within the last `max_span_s` seconds after the latest risk event, per decode-to-prefill gate.

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
