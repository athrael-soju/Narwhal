# Reconcile and accept

## 10. Reconcile the client and router evidence

`requests.jsonl` must contain one terminal record for every scheduled offer.

Each record contains:

* `client_rid`;
* request sequence;
* scheduled start;
* actual start;
* HTTP status;
* error details;
* input token count;
* output token count;
* TTFT;
* TPOT.

SSE token accounting includes identified token IDs whose text is empty and tokens emitted only as reasoning output.

A response is complete only when all of the following are true:

* every stream event contains one identified token;
* the stream terminates with a length finish;
* the stream terminates with `[DONE]`;
* final usage counts match the observed token counts.

Client TTFT runs from HTTP dispatch to the first identified output token.

Client TPOT is:

```text
time(first identified token -> last identified token)
----------------------------------------------------
          completed_output_tokens - 1
```

Latency percentiles include complete responses only.

Join deployment-client records to the router journal using:

```text
client_rid
```

Every sent offer should reconcile to exactly one terminal class.

## 11. Validate throughput and client-side measurement integrity

`summary.json` reports:

* completed-request throughput;
* output-token throughput;
* SLO-qualified-request throughput.

Each uses elapsed time through the final response drain as its denominator.

Before assigning a serving throughput ceiling, compare:

* deployment-client CPU time;
* deployment-client peak memory;
* deployment-client scheduling lag;
* workstation interface counters from `network-before.json`;
* workstation interface counters from `network-after.json`;
* router metrics;
* engine metrics.

Use the state snapshots to confirm router state around the measured run:

```text
state-before.json
state-after-warmup.json
state-after.json
```

Verify admission queues and resident work:

1. before warmup;
2. after warmup;
3. after the measured run.

## 12. Preserve run integrity

Every trial run uses a new directory with:

```text
directory mode = 0700
file mode      = 0600
```

The load helper executes directly from the management checkout.

Its recorded digest therefore identifies local helper modifications separately from the installed router revision.

After reconciling client and router records, query the dashboard series and run the post-load KV ring described in [Gate G](../deploy/07-Serve-and-Measure.md#run-the-initial-capacity-trial).

## 13. Record deployment acceptance

The deployment acceptance record must identify the highest tested offered rate that satisfied the candidate client target.

Record at minimum:

* highest passing offered rate;
* request shape;
* SSH route;
* preflight revision;
* retained artefact paths.
