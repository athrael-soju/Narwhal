# Reconcile and accept

Reconcile the client's per-request records with the router journal, then record acceptance.

## 10. Join client offers to the router journal

The client writes one terminal record per scheduled request to `requests.jsonl`, keyed by request sequence and `client_rid`. Requests the client did not send get a record with `sent: false` and outcome `client_schedule_miss`, so every scheduled request is counted in the offered total. Sent requests record their scheduled and actual start, HTTP status, output count, and input count, TTFT, TPOT, and error detail where available.

A response only counts as complete if:

- every stream event identifies its output tokens by ID (an event that bundles several tokens is allowed and counted in `batched_token_events`)
- the stream ends with a length finish and `[DONE]`
- the final usage figures match the tokens the client counted

Tokens are counted as in the measurement contract: anything with a token ID counts, including empty-text and reasoning-only tokens. TTFT and TPOT follow the client definitions in the [measurement contract](01-Profile.md#1-define-the-measurement-contract). Latency percentiles come from complete responses only.

Join the client records to the router journal on `client_rid`. Each sent request matches exactly one terminal class.

### Attainment

Deployment attainment comes from the client's `summary.json`, as `within_candidate_limits / offered`.

The numerator counts complete responses that met the TTFT limit and, if they have more than one output token, the TPOT limit. The denominator is every scheduled, scored request, including any the client never sent. A request that times out or disconnects after partial output is a miss, even if it has timings and token counts. The warmup request in `warmup.json` is not scored.

The journal explains what happened to the requests that were sent and does not set the denominator.

## 11. Check throughput and client limits

`summary.json` reports three throughputs: completed requests, completed output tokens, and SLO-qualified requests. Each is divided by the elapsed time up to the end of the final response drain.

Rule out a client bottleneck before assigning a serving ceiling. Check:

- the client's CPU time, peak memory, and scheduling lag
- the workstation's interface counters in `network-before.json` and `network-after.json`
- router and engine metrics
- admission queues and resident work in the three state snapshots: `state-before.json` (before warmup), `state-after-warmup.json`, and `state-after.json` (after the measured run)

## 12. Record integrity and final checks

Each trial creates a new directory with mode `0700` and writes its files with mode `0600`. When the helper runs from the management checkout, its digest is recorded separately from the router revision installed on the router host, so local changes to the helper appear in the record.

When the client and router records agree, query the dashboard series and run the post-load KV ring check described in [Run the initial capacity trial](../deploy/07-Serve-and-Measure.md#run-the-initial-capacity-trial).

Preflight and the ring check confirm that every transfer path the role configuration allows moves KV and produces tokens. They do not confirm identical output when roles change while requests are still resident. Run an exact-output comparison to confirm that.

## 13. Record deployment acceptance

Record the highest offered rate that met the candidate limits, along with:

- the request shape
- the SSH route
- the preflight revision
- paths to the retained artifacts
