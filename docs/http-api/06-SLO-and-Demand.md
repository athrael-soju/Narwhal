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

Each bucket keeps three counts over completed, failed, expired, and predictively refused requests: TTFT met, TPOT met, and total.

A query over a window counts the bucket at the window's start in full.

## Demand accounting

One demand window is `controller.reactive.window_s` seconds long.

### Unsized offers

`demand_history.unsized` reports two counts:

| Field          | Meaning                                           |
| -------------- | ------------------------------------------------- |
| `pending`      | Request bodies the router is reading              |
| `observations` | Retained offers that ended before workload sizing |

Each unsized offer takes the mean price of the parsed offers in the same span.

### Demand completeness

Demand in a span is [incomplete](../concepts/02-Role-Control.md#incomplete-demand) when any of these is true:

- The fleet has zero engine profiles.
- The span has unsized offers and zero parsed offers.
- An uncapped offer's prompt bucket has zero delivered outputs to estimate its output from.
- An overflow cohort's output cap is zero.
- An expected decode shape has zero capacity at both its bucketed and exact length.

The demand estimate includes prompts longer than the profile's measured range, `prefill_max_tokens`. The router prices these prompts by extending the [prefill fit](../telemetry/02-Profiles.md#prefill-price-derived-from-the-profile) to their length.

### Input-size repricing

Demand history records each parsed offer at its arrival timestamp, with this input size:

| Offer                        | Recorded input size                         |
| ---------------------------- | ------------------------------------------- |
| On entry                     | Local input-size estimate                   |
| Tokenized after admission    | Tokenization result, replacing the estimate |
| Rejected before tokenization | Local input-size estimate                   |

### Bucketing and retention

Demand history buckets and retains its evidence as follows:

| Property                      | Value                                                                      |
| ----------------------------- | -------------------------------------------------------------------------- |
| Bucket width                  | Smallest of one second, the controller step, and the minimum evidence span |
| Exact request shapes          | Up to 128 per bucket                                                       |
| Overflow cohort               | One per bucket                                                             |
| Unsized offers                | One shape per bucket                                                       |
| Arrival and residency data    | Kept for one demand window                                                 |
| Completed output observations | Kept for four demand windows                                               |
| Pruning                       | On each evidence write                                                     |

`demand_history` holds one object for each history (`unsized`, `arrivals`, `expected_decode`, `observed_decode`, and `residency`), each with these fields:

| Field                   | Meaning                          | Metric, labelled by `window`                   |
| ----------------------- | -------------------------------- | ---------------------------------------------- |
| `bucket_s`              | Bucket width                     |                                                |
| `retained_s`            | Retention span                   |                                                |
| `cells`                 | Retained cells                   | `narwhal_demand_history_cells`                 |
| `cell_limit`            | Cell limit                       | `narwhal_demand_history_cell_limit`            |
| `observations`          | Counted observations             | `narwhal_demand_history_observations`          |
| `overflow_observations` | Observations in overflow cohorts | `narwhal_demand_history_overflow_observations` |

### Overflow

An overflow cohort:

- keeps every request count
- takes its price from its largest input length and requested output length
- marks decode demand incomplete when its requested output length is zero
- keeps the output estimates learned before the overflow until every overflow cohort leaves output history
- contributes its full count when it crosses a window boundary

### Decision snapshots

A reactive decision snapshot contains profile coefficients, demand, phase pressure, output estimates, and resident work.

### Prefill recovery ratio

When `queued_prefill_s > 0` and at least one engine has the prefill role, the prefill recovery ratio is:

```text
recovery_prefill_ratio = max(
  observed prefill pressure,
  (resident prefill seconds + queued prefill seconds)
    / (current prefill engine count * TTFT SLO seconds)
)
```

Otherwise, `recovery_prefill_ratio` equals the observed prefill pressure.

Decisions made with incomplete demand report both observed phase ratios, `recovery_prefill_ratio`, and `queued_prefill_s`.

In those decisions, `decision_basis` is one of:

```text
prefill_pressure_recovery
decode_pressure_recovery
```

Source-pressure and movement-gate checks use demand at full precision. State and journal records round it.

Role floors, cooldown, dwell, KV limits, and the [movement and confirmation gates](../configuration/02-Serving-and-Role-Control.md#role-control) constrain each move.

### Decode recovery ratio

`recovery_decode_ratio` equals the observed decode pressure. Role control prices decode capacity from engine profiles without the router's [engine seats](../configuration/02-Serving-and-Role-Control.md#engine-seats).

Decisions held before candidate scoring, while they wait for demand history or fleet profiles, report `recovery_decode_ratio`.

### Consolidation evidence

`demand_evidence` fields:

| Field                     | Meaning                                                                                       |
| ------------------------- | --------------------------------------------------------------------------------------------- |
| `span_s`                  | Time span of the counted arrivals                                                             |
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
