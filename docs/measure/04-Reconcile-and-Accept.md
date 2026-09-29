# Reconcile and accept

## 10. Join client offers to the router journal

`requests.jsonl` holds one terminal record per scheduled offer, identified by request sequence and `client_rid`. A scheduling miss carries `sent: false` and outcome `client_schedule_miss`.

| Record                                    | Contents                                                                              |
| ----------------------------------------- | ------------------------------------------------------------------------------------- |
| Scheduling miss                           | `sent: false` and outcome `client_schedule_miss`                                      |
| Sent offer                                | Scheduled and actual starts, HTTP status, and output count                            |
| Sent offer, when available at termination | Input count, time to first token (TTFT), time per output token (TPOT), or error detail |

Tokens with empty text and tokens emitted only as reasoning output still count, as long as they carry token IDs.

A response is complete when:

- Every output-bearing stream event carries identified token IDs.
- The stream reaches a length finish and `[DONE]`.
- The completed output length equals the requested output length.
- Final usage matches the observed token counts.

The helper requests `stream_interval: 1` and counts multi-token events in `batched_token_events`.

Client TTFT runs from HTTP dispatch to the first identified output token. Client TPOT is:

```text
time(first identified token -> last identified token)
----------------------------------------------------
          completed_output_tokens - 1
```

Both metrics use the deployment-client boundaries defined in [Define the measurement contract](01-Profile.md#1-define-the-measurement-contract). Compute latency percentiles from complete responses.

The helper sends each offer's `client_rid` as its `x-request-id` header, and the router journal stores that value as `client_rid`. Join client records to the journal on `client_rid`. Each sent offer gets one terminal class. For runner points, the [benchmark evidence collector](06-Benchmark-Evidence.md) compares the two sides.

Deployment attainment is `within_candidate_limits / offered` from the client `summary.json`:

| Term        | Counts                                                                                   |
| ----------- | ---------------------------------------------------------------------------------------- |
| Numerator   | Completed responses within the TTFT limit, and within the TPOT limit for outputs over one token |
| Denominator | Every scheduled scored offer, including unsent scheduling misses                         |

A timeout or disconnect after partial output counts as a miss. The helper scores only measured offers; `warmup.json` is excluded. Joined journal rows explain sent offers.

## 11. Check throughput denominators and client limits

`summary.json` reports throughput by dividing completed requests, output tokens, and SLO-qualified requests by elapsed time through the final response drain.

Before assigning a serving throughput ceiling, compare:

* deployment-client CPU time, peak memory, and scheduling lag;
* workstation interface counters from `network-before.json` and `network-after.json`;
* router and engine metrics.

Check admission queues and resident work in `state-before.json`, `state-after-warmup.json`, and `state-after.json`.

## 12. Preserve run integrity

Each trial goes to a new directory (mode `0700`) with files at `0600`. The helper records the management checkout digest apart from the installed router revision.

### Confirm KV transfer after load

After reconciling the client and router records, query the dashboard series and run the [post-load KV ring](../deploy/07-Serve-and-Measure.md#run-the-initial-capacity-trial).

Preflight and the post-load ring show that the tested paths transfer KV and produce tokens. To accept correctness across role changes with resident requests, compare exact output.

## 13. Record deployment acceptance

Record the highest tested offered rate that met the candidate client target. Record with it the request shape, SSH route, preflight revision, and artifact paths.
