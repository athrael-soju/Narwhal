# Reconcile and accept

## 10. Join client offers to the router journal

`requests.jsonl` holds one terminal record per scheduled offer, identified by request sequence and `client_rid`.

| Record                                    | Contents                                                                              |
| ----------------------------------------- | ------------------------------------------------------------------------------------- |
| Scheduling miss                           | `sent: false` and outcome `client_schedule_miss`                                      |
| Sent offer                                | Scheduled and actual starts, HTTP status, and output count                            |
| Sent offer, when available at termination | Input count, time to first token (TTFT), time per output token (TPOT), or error detail |

Every token with a token ID counts toward output, including empty-text and reasoning-only tokens.

A response is complete when:

- Every output-bearing stream event carries identified token IDs.
- The stream reaches a length finish and `[DONE]`.
- The completed output length equals the requested output length.
- Final usage matches the observed token counts.

`batched_token_events` counts stream events that carry more than one token under the helper's `stream_interval: 1`.

[Client TTFT](01-Profile.md#1-define-the-measurement-contract) runs from HTTP dispatch to the first identified output token.

Client TPOT is:

```text
time(first identified token -> last identified token)
----------------------------------------------------
          completed_output_tokens - 1
```

Compute latency percentiles from complete responses.

Join client records to the router journal on `client_rid`:

| Side | `client_rid` |
| --- | --- |
| Client | Sent as the offer's `x-request-id` header |
| Router journal | Stored from `x-request-id` |

For runner points, the [benchmark evidence collector](06-Benchmark-Evidence.md) runs this join.

Deployment attainment is `within_candidate_limits / offered` from the client `summary.json`:

| Term        | Counts                                                                                   |
| ----------- | ---------------------------------------------------------------------------------------- |
| Numerator   | Completed responses within the TTFT limit, and within the TPOT limit for outputs over one token |
| Denominator | Every scheduled scored offer, including unsent scheduling misses                         |

- A timeout or disconnect after partial output counts as a miss.
- The warmup in `warmup.json` is unscored.

## 11. Check throughput denominators and client limits

`summary.json` reports three throughputs: completed requests, output tokens, and SLO-qualified requests, each divided by the elapsed time through the final response drain.

Before assigning a serving throughput ceiling, compare:

* deployment-client CPU time, peak memory, and scheduling lag;
* workstation interface counters from `network-before.json` and `network-after.json`;
* router and engine metrics.

Check admission queues and resident work in `state-before.json`, `state-after-warmup.json`, and `state-after.json`.

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

Correctness across role changes with resident requests requires an exact-output comparison.

## 13. Record deployment acceptance

Record the highest tested offered rate that met the candidate client target, with its request shape, SSH route, preflight revision, and artifact paths.
