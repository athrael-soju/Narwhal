# Reconcile and accept

After a trial you have two independent accounts of every request, one from the client and one from the router. This page covers making them agree and then recording the result.

## 10. Join client offers to the router journal

The client writes one terminal record per scheduled request to `requests.jsonl`, keyed by request sequence and `client_rid`. Requests the client never sent still get a record, with `sent: false` and outcome `client_schedule_miss`, so the offered count stays intact. Sent requests record their scheduled and actual start, HTTP status, output count, and whatever input count, TTFT, TPOT, or error detail was available when the request ended.

A response only counts as complete if:

- every stream event identifies its output tokens by ID (an event that bundles several tokens is allowed and counted in `batched_token_events`)
- the stream ends with a length finish and `[DONE]`
- the final usage figures match the tokens the client counted

Tokens are counted the same way as in the measurement contract: anything with a token ID counts, including empty-text and reasoning-only tokens. TTFT and TPOT follow the client definitions in the [measurement contract](01-Profile.md#1-define-the-measurement-contract). Latency percentiles come from complete responses only.

Join the client records to the router journal on `client_rid`, so that each sent request ends up with exactly one terminal class.

### Attainment

Deployment attainment comes from the client's `summary.json`, as `within_candidate_limits / offered`.

The numerator counts complete responses that met the TTFT limit and, if they have more than one output token, the TPOT limit. The denominator is every scheduled, scored request, including any the client never sent. A request that times out or disconnects after partial output is a miss, even if it has timings and token counts. The warmup request in `warmup.json` isn't scored.

The denominator always comes from the client. Use the journal to explain what happened to the requests that were sent.

## 11. Check throughput and client limits

`summary.json` reports three throughputs: completed requests, completed output tokens, and SLO-qualified requests. Each is divided by the elapsed time up to the end of the final response drain.

A throughput number only describes the fleet if the client wasn't the bottleneck. Before you assign a serving ceiling, check:

- the client's CPU time, peak memory, and scheduling lag
- the workstation's interface counters in `network-before.json` and `network-after.json`
- router and engine metrics

Also look at the admission queues and resident work in the three state snapshots: `state-before.json` (before warmup), `state-after-warmup.json`, and `state-after.json` (after the measured run).

## 12. Preserve run integrity

Each trial creates a new directory with mode `0700` and writes its files with mode `0600`. If you run the helper from the management checkout, its digest is recorded separately from the router revision installed on the router host, so a local change to the helper shows up in the record.

Once the client and router records agree, query the dashboard series and run the post-load KV ring check described in [Gate G](../deploy/07-Serve-and-Measure.md#run-the-initial-capacity-trial).

Preflight and the ring check show that every transfer path the role configuration allows can move KV and produce tokens. They don't show that output stays identical when roles change while requests are still resident. If you need that guarantee, run an exact-output comparison.

## 13. Record deployment acceptance

Record the highest offered rate that met the candidate target, along with the request shape, SSH route, preflight revision, and paths to the retained artifacts.
