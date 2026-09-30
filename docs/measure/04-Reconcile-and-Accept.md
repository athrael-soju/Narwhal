# Reconcile and accept

## 10. Join client offers to the router journal

`requests.jsonl` holds one terminal record per scheduled offer, identified by request sequence and `client_rid`.

A scheduling miss records:

- `sent: false`.
- Outcome `client_schedule_miss`.

A sent offer records:

- Scheduled and actual starts, HTTP status, and output count.
- Input count, time to first token (TTFT), time per output token (TPOT), and error detail when available at termination.

Every token with a token ID counts toward output, including empty-text and reasoning-only tokens.

A response is complete when:

- Every output-bearing stream event carries identified token IDs.
- The stream reaches a length finish and `[DONE]`.
- The completed output length equals the requested output length.
- Final usage matches the observed token counts.

`batched_token_events` counts stream events that carry more than one token under the helper's `stream_interval: 1`.

[Client TTFT](01-Profile.md#1-define-the-measurement-contract) is the time from HTTP dispatch to the first identified output token.

Client TPOT is:

```text
time(first identified token -> last identified token)
----------------------------------------------------
          completed_output_tokens - 1
```

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
| --- | --- |
| Trial directory | `0700` |
| Files in the trial directory | `0600` |

The helper records the management checkout digest and the installed router revision separately.

### Confirm KV transfer after load

1. Query the dashboard series.
2. Run the [post-load KV ring](../deploy/07-Serve-and-Measure.md#run-the-initial-capacity-trial).
3. Compare exact output to verify correctness across role changes with resident requests.

## 13. Record deployment acceptance

Record the highest tested offered rate that met the candidate client target:

- request shape
- SSH route
- preflight revision
- artifact paths
