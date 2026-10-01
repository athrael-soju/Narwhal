---
description: SLO attainment per time bucket and demand accounting in the Narwhal state API.
---

# SLO attainment and demand accounting

## SLO attainment

`attainment` reports SLO results per time bucket:

| Field             | Meaning                                                                 |
| ----------------- | ----------------------------------------------------------------------- |
| `bucket_s`        | Bucket width, `controller.monitor_interval_s`                           |
| `retained_s`      | Retention span, four demand windows counted back from the newest bucket |
| `covered_s`       | Age of the oldest retained bucket, capped at `retained_s`               |
| `buckets`         | Retained buckets                                                        |
| `outcomes`        | Requests counted in retained buckets                                    |
| `pruned_buckets`  | Buckets aged out of retention                                           |
| `pruned_outcomes` | Requests in pruned buckets                                              |

| Property          | Value                                                |
| ----------------- | ---------------------------------------------------- |
| Counted requests  | Completed, failed, expired, and predictively refused |
| Counts per bucket | TTFT-met, TPOT-met, and total requests               |
| Window queries    | Include the whole boundary bucket                    |

## Demand accounting

One demand window is `controller.reactive.window_s` seconds long.

### Unsized offers

`demand_history.unsized` reports two counts:

- `pending`: request bodies being read.
- `observations`: retained offers that terminated before workload sizing.

### Input-size repricing

| Offer                        | Input size in demand history                                    |
| ---------------------------- | --------------------------------------------------------------- |
| Parsed, at entry             | Local input-size estimate                                       |
| Tokenized after admission    | Tokenization result, recorded at the original arrival timestamp |
| Rejected before tokenization | Local input-size estimate                                       |

### Bucketing and retention

| Property                      | Value                                                                      |
| ----------------------------- | -------------------------------------------------------------------------- |
| Bucket width                  | Smallest of one second, the controller step, and the minimum evidence span |
| Exact request shapes          | Up to 128 per bucket                                                       |
| Overflow cohort               | One per bucket                                                             |
| Unsized offers                | One shape per time bucket                                                  |
| Arrival and residency data    | Kept for one demand window                                                 |
| Completed output observations | Kept for four demand windows                                               |
| Pruning                       | On each evidence write                                                     |

`demand_history` holds one object per history: `unsized`, `arrivals`, `expected_decode`, `observed_decode`, and `residency`.

| Field                   | Meaning                  | Metric, labelled by `window`                   |
| ----------------------- | ------------------------ | ---------------------------------------------- |
| `bucket_s`              | Bucket width             |                                                |
| `retained_s`            | Retention span           |                                                |
| `cells`                 | Retained cells           | `narwhal_demand_history_cells`                 |
| `cell_limit`            | Cell limit               | `narwhal_demand_history_cell_limit`            |
| `observations`          | Counted observations     | `narwhal_demand_history_observations`          |
| `overflow_observations` | Observations in overflow | `narwhal_demand_history_overflow_observations` |

### Overflow

An overflow cohort:

- keeps every request count
- is priced from its largest input length and requested output length
- marks decode demand incomplete when its requested output length is zero
- keeps the output estimates learned before the overflow until every overflow cohort leaves output history
- contributes its full count when it crosses a window boundary

### Decision snapshots

A reactive decision snapshot contains:

- profile coefficients
- demand
- phase pressure
- output estimates
- resident work

### Prefill recovery ratio

The prefill recovery ratio is computed as follows when `queued_prefill_s > 0` and at least one engine has the prefill role:

```text
recovery_prefill_ratio = max(
  observed prefill pressure,
  (resident prefill seconds + queued prefill seconds)
    / (current prefill engine count * TTFT SLO seconds)
)
```

Otherwise, `recovery_prefill_ratio` equals the observed prefill pressure.

Incomplete-demand decisions expose:

- both observed phase ratios
- `recovery_prefill_ratio`
- `queued_prefill_s`

`decision_basis` in incomplete-demand decisions is one of:

```text
prefill_pressure_recovery
decode_pressure_recovery
```

Demand precision by consumer:

| Consumer                                 | Precision      |
| ---------------------------------------- | -------------- |
| Source-pressure and movement-gate checks | Full precision |
| State and journal records                | Rounded        |

Role floors, cooldown, dwell, KV limits, and the [movement and confirmation gates](../configuration/02-Serving-and-Role-Control.md#7-role-control) constrain each move.

### Decode recovery ratio

The decode recovery ratio is computed as follows when `serving.decode_concurrency` is positive and at least one engine has the decode role:

```text
recovery_decode_ratio = max(
  observed decode pressure,
  (decode residents on decode-role engines + requests waiting for a decode slot)
    / (current decode engine count * serving.decode_concurrency)
)
```

Otherwise, `recovery_decode_ratio` equals the observed decode pressure.

Decisions held for demand history or fleet profiles before candidate scoring add `recovery_decode_ratio`.

### Consolidation evidence

`demand_evidence` fields:

| Field                     | Meaning                                                                                       |
| ------------------------- | --------------------------------------------------------------------------------------------- |
| `span_s`                  | Elapsed span of the counted arrival evidence                                                  |
| `arrivals`                | Arrivals since the later of the latest risk event and `max_span_s` seconds ago                |
| `required_span_s`         | Span that closes the window, `controller.reactive.evidence_span_s`                            |
| `required_arrivals`       | Arrival count that closes the window, `controller.reactive.evidence_min_arrivals`             |
| `max_span_s`              | Longest lookback, `controller.reactive.evidence_max_span_s`                                   |
| `closed`                  | `true` when the window meets `required_span_s` and `required_arrivals`, or spans `max_span_s` |
| `risk_kind`               | Newest risk event: `first_token_timeout` or `p_to_d_recovery`                                 |
| `risk_age_s`              | Seconds since the newest risk event                                                           |
| `risk_events`             | Risk-event counts by kind                                                                     |
| `short_decode_engines`    | Decode demand over `required_span_s`, in engines                                              |
| `long_decode_engines`     | Decode demand from the latest full-window estimate, in engines                                |
| `trend_ratio`             | `short_decode_engines` divided by `long_decode_engines`                                       |
| `envelope_decode_engines` | Larger of `short_decode_engines` and `long_decode_engines`                                    |
| `blocked_gate`            | `none`, `risk`, `evidence`, or `trend`                                                        |
