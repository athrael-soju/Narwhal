# Reconcile and accept

## 10. Join client offers to the router journal

`requests.jsonl` writes one terminal record per scheduled offer, identified by request sequence and `client_rid`. A client scheduling miss carries `sent: false` and outcome `client_schedule_miss`, preserving the offered count. Sent offers record scheduled and actual starts, HTTP status, output count, and any input count, TTFT, TPOT, or error detail available at termination.

SSE token accounting includes identified token IDs whose text is empty and tokens emitted only as reasoning output.

Mark a response complete only when all of these conditions hold:

- Each output-bearing stream event carries one identified token.
- The stream reaches a length finish and `[DONE]`.
- Final usage matches the observed token counts.

Client TTFT runs from HTTP dispatch to the first identified output token.

Client TPOT is:

```text
time(first identified token -> last identified token)
----------------------------------------------------
          completed_output_tokens - 1
```

Compute latency percentiles from complete responses.

Join deployment-client records to the router journal on `client_rid`, assigning each sent offer exactly one terminal class.

## 11. Check throughput denominators and client limits

`summary.json` divides completed requests, completed output tokens, and SLO-qualified requests by elapsed time through the final response drain to report their throughput.

Before assigning a serving throughput ceiling, compare:

* deployment-client CPU time;
* deployment-client peak memory;
* deployment-client scheduling lag;
* workstation interface counters from `network-before.json`;
* workstation interface counters from `network-after.json`;
* router metrics;
* engine metrics.

Inspect admission queues and resident work in `state-before.json`, `state-after-warmup.json`, and `state-after.json` to confirm router state before warmup, after warmup, and after the measured run.

## 12. Preserve run integrity

Each trial writes a new directory with mode `0700` and files with mode `0600`. Running the load helper from the management checkout records its local digest separately from the installed router revision.

After reconciling client and router records, query the dashboard series and run the post-load KV ring described in [Gate G](../deploy/07-Serve-and-Measure.md#run-the-initial-capacity-trial).

Preflight and the post-load ring establish that the tested role-permitted paths transfer KV and produce tokens. Exact-output correctness across role changes with resident requests requires separate evidence.

## 13. Record deployment acceptance

Record the highest tested offered rate meeting the candidate client target with its request shape, SSH route, preflight revision, and retained artefact paths.
