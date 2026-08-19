# Serving KPIs

Narwhal separates prefill and decode quality because they describe different parts of the user experience and consume different fleet capacity. Measure both for every run. Throughput by itself can conceal a fleet that is serving many requests too slowly.

## Primary latency metrics

| KPI                | What it measures                                                                                       | Formula                                                      | Main capacity phase  |
| ------------------ | ------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------ | -------------------- |
| TTFT               | Time to first token. How long the user waits for generation to begin                                   | prefill queue delay + prefill compute                        | Prefill              |
| TPOT / ITL         | Time per output token (the mean inter-token latency). How evenly a response streams after it begins    | `(last token time - first token time) / (output tokens - 1)` | Decode               |
| First-byte latency | The client-visible wait until the streaming response starts to arrive                                  | TTFT + KV transfer + decode queueing                         | The P/D handoff path |
| End-to-end latency | Time from request arrival to the completed response                                                    | first-byte latency + remaining generation time               | Both                 |

TTFT ends when the prefill instance produces `o1`. Because it measures prefill quality, a high TTFT points at prefill queues, long prompts, or too little prefill capacity. The router journals this value as `ttft_s`.

TPOT is the mean gap between output tokens from the same decode instance. A response with fewer than two output tokens has no token-to-token gap, so its TPOT is `null`. A high TPOT points at decode pressure from oversized batches or too little decode capacity. The router journals this value as `tpot_s`.

`first_byte_s` differs from `ttft_s`. It reports what the client sees, and on a crossed request it exceeds `ttft_s` by the KV transfer and the decode-queue wait that follow prefill. Use `first_byte_s` to judge client experience. TTFT and TPOT show which serving phase needs capacity.

## SLO attainment and goodput

Set separate SLOs for TTFT and TPOT. A request attains the serving SLO only when it completes its requested output and meets both targets.

```
complete request
and ttft_s <= TTFT SLO
and tpot_s <= TPOT SLO
a null tpot_s (fewer than two output tokens) does not fail the target
```

The percentage of offered requests that meet every line of that condition is the **SLO attainment**. Report it with TTFT and TPOT percentiles, because averages hide tail latency. The Prometheus histograms export both distributions, and the `narwhal-report` percentile table covers the KV handoff.

The highest sustained request rate that still meets both TTFT and TPOT SLOs at the chosen attainment target is the **goodput**, with 90% as the default target. Only completed, timely work counts toward it, so a run's goodput measures its usable capacity. Raw throughput can increase while goodput decreases when a fleet accepts more work than it can serve within its latency budgets.

For interactive chat and code completion, a tight TTFT avoids an initial pause and a tight TPOT keeps the token stream even. Long summarization workloads tolerate a looser TTFT budget but still need enough decode capacity to meet their TPOT target.

## Operational diagnostics

These split into two kinds. Live metrics are queryable on the router's `/metrics` while serving ([Observability](Observability.md) lists every series). Post-run report columns are computed by `narwhal-report` from the journal after a run, and a dashboard cannot graph them.

### Live metrics

| KPI                              | Why it matters                                                                                                   |
| -------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| Error rate                       | Errors miss goodput regardless of their latency.                                                                 |
| Prefill/decode pool load         | Each pool's pressure relative to its SLO-based target. Values above `1.0` exceed the modeled budget.             |
| Resident requests / queue depth  | Leading indicator of pressure on an instance or phase. Read it alongside TTFT and TPOT as a diagnostic.          |
| Re-role events                   | Shows whether the adaptive controller responds when the workload's phase mix changes.                            |
| Flip reversals (thrash)          | Detects unstable P/D role changes that undo each other.                                                          |
| Requests resident during a flip  | Quantifies the work exposed to a re-role operation and its potential latency cost.                               |
| Ejected instances                | Nonzero means the breaker currently excludes capacity from scheduling. Treat it as an availability signal.       |
| Placement regret                 | Scheduling-efficiency diagnostic relative to the placement's own lower-bound cost.                               |

### Post-run report columns

| KPI                              | Why it matters                                                                                                   |
| -------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| Short-response rate              | Outputs short of `wanted_len` miss goodput regardless of their latency.                                          |
| KV-transfer time                 | Separates the cost of a crossed prefill/decode handoff from compute and queueing.                                |
| Time-to-adapt                    | Shows whether the adaptive controller responds promptly when the workload's phase mix changes.                   |

The request journal is the source of record for request-level KPIs. See [Api](Api.md) for its fields and `narwhal-bench --score-journal` for scoring a recorded run against the configured SLOs.
