---
description: Reconcile client offers with the Narwhal router journal and record deployment acceptance.
---

# Reconcile and accept

## 10. Join client offers to the router journal

`requests.jsonl` holds one terminal record per scheduled offer, identified by request sequence and `client_rid`.

| Offer | Record |
| --- | --- |
| Scheduling miss | `sent: false` and outcome `client_schedule_miss` |
| Sent offer | Scheduled and actual starts, HTTP status, output count, input count, time to first token (TTFT), time per output token (TPOT), and any error detail at termination |

A response is complete when:

- every output-bearing stream event carries identified token IDs
- the stream reaches a length finish and `[DONE]`
- the completed output length equals the requested output length
- final usage matches the observed token counts

`batched_token_events` counts stream events that carry more than one token under the helper's `stream_interval: 1`.

Client TTFT and TPOT follow the [measurement contract](01-Profile.md#1-define-the-measurement-contract).

Latency percentiles use complete responses.

Join key `client_rid`:

| Side | `client_rid` |
| --- | --- |
| Client | Sent as the offer's `x-request-id` header |
| Router journal | Stored from `x-request-id` |

For runner points, the [benchmark evidence collector](06-Benchmark-Evidence.md) runs this join.

Deployment attainment is `within_candidate_limits / offered` from the client `summary.json`:

| Term        | Counts                                                                                   |
| ----------- | ---------------------------------------------------------------------------------------- |
| Numerator   | Completed responses within the TTFT limit and, for outputs over one token, the TPOT limit |
| Denominator | Every scheduled scored offer, including unsent scheduling misses                         |

- A timeout or disconnect after partial output counts as a miss.
- The warmup in `warmup.json` is excluded from scoring.

## 11. Check throughput denominators and client limits

Each `summary.json` throughput divides by the elapsed time through the final response drain:

| Throughput | Numerator |
| --- | --- |
| Request throughput | Completed requests |
| Output token throughput | Output tokens |
| SLO-qualified throughput | SLO-qualified requests |

Before assigning a serving throughput ceiling, compare:

- deployment-client CPU time, peak memory, and scheduling lag
- workstation interface counters from `network-before.json` and `network-after.json`
- router and engine metrics
- admission queues and resident work in `state-before.json`, `state-after-warmup.json`, and `state-after.json`

## 12. Preserve run integrity

Each trial writes to a new directory:

| Path | Mode |
| --- | :---: |
| Trial directory | `0700` |
| Files in the trial directory | `0600` |

| Record | Build identity |
| --- | --- |
| Trial `manifest.json` | Management checkout Git revision and helper SHA-256 |
| Router journal `meta` row | Installed router package version, Git description, and source digest |

### Confirm KV transfer after load

1. Query the dashboard series.
2. Run the [post-load KV ring](../deploy/07-Serve-and-Measure.md#run-the-initial-capacity-trial).
3. Compare exact output across role changes with resident requests.

## 13. Record deployment acceptance

Record the highest tested offered rate that met the candidate client target, with:

- request shape
- SSH route
- preflight revision
- artifact paths
