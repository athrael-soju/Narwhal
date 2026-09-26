# Request journal

## Diagnose a request from the journal

Pass `--journal <path>` to place the JSON Lines journal at a chosen path; its default is `journal.jsonl` beside `profiles.path`. Narwhal appends records under a process-specific `run` identifier, separating restarts and standby takeovers in a shared file.

The first row identifies the journal contract and the build that produced it:

```json
{"meta":{"schema":"narwhal.journal","schema_version":1,"package":"narwhal-inference","version":"0.1.0","git":"<commit>","source":"sha256:...","token_accounting":"token_ids"}}
```

`source` is the SHA-256 digest of the installed package's Python files. Identical Python source produces the same digest from a checkout, deployment, or wheel.

### Terminal request records

Narwhal closes each original completion request with one terminal row, attaching retries and the final invalid, rejected, expired, failed, cancelled, refused, or completed outcome to that request.

| Field                                        | Meaning                                                                                                                                                               |
| -------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `run`, `rid`, `client_rid`                   | Router process, Narwhal request ID, and optional caller request ID.                                                                                                   |
| `arrived`                                    | Arrival time on the process monotonic clock. Compare this value only within one `run`.                                                                                |
| `input_len`, `output_len`, `wanted_len`      | Prompt tokens, returned tokens, and requested output tokens. For a cancellation, `output_len` records delivered tokens when they can be measured.                     |
| `ttft_s`, `tpot_s`, `first_byte_s`           | Router-side prefill, decode, and first-visible-output timing.                                                                                                         |
| `prefill_iid`, `decode_iid`                  | Engines selected for the prefill and decode legs.                                                                                                                     |
| `crossed`                                    | Whether decode consumed KV produced by the recorded prefill engine.                                                                                                   |
| `token_accounting`                           | Decode-output accounting mode. `token_ids` provides exact per-token identity; every other dialect reports `unavailable`.                                              |
| `refused`, `refused_cause`                   | Predictive refusal flag and reason: `prompt` (prompt alone exceeds the TTFT budget), `queue` (total predicted TTFT exceeds the budget although the prompt alone fits), or `aggregate_unpriced` (no calibrated aggregate prefill price). See [admission policy](../configuration/02-Serving-and-Role-Control.md#41-global-admission). |
| `cancelled`, `cancelled_phase`               | Client disconnect and the phase in which it occurred: `admission`, `queue`, `backoff`, `prefill`, or `decode`.                                                        |
| `terminal`                                   | Final request state: `completed`, `failed`, `refused`, `rejected`, `expired`, `invalid`, or `cancelled`.                                                              |
| `input_sized`                                | Whether local input sizing completed. When false, the body terminated before sizing and `input_len: 0` records that early exit.                                       |
| `attempts`, `decode_attempts`                | Prefill and decode dispatch counts for the original request.                                                                                                          |
| `attempt_failures`                           | Bounded records for failed attempts, including failures followed by successful retries.                                                                               |
| `queue_wait_s`, `duration_s`                 | Total admission and dispatch wait, and complete request lifetime on the router clock.                                                                                 |
| `decode_tpot_s`                              | Time from first to last observed output token divided by `output_tokens - 1`. Null when fewer than two tokens were observed or exact token accounting is unavailable. |
| `decode_tokens_observed`, `upstream_seconds` | Tokens observed across every attempt and summed HTTP-leg duration, including failed work, transfer time, and waiting.                                                 |
| `error`                                      | Failure or refusal detail. Successful and cancelled requests use null.                                                                                                |

Each `attempt_failures` entry can record:

- monotonic time;
- attempt number;
- request phase;
- prefill and decode engine IDs;
- exception type, message, and status;
- transient classification;
- visible-output state;
- retry decision;
- scheduled backoff.

Narwhal retains at most `serving.max_attempts` failure entries and caps messages at 240 characters. A retry decision records the scheduled action, which a client cancellation can interrupt during backoff before dispatch.

### Attainment accounting

For deployment acceptance, score the [client's scheduled offers](../measure/04-Reconcile-and-Accept.md#10-join-client-offers-to-the-router-journal) against its TTFT and TPOT limits. Only completed client responses within the applicable limits pass. All other scored offers are misses. The client includes unsent offers in the denominator and excludes the unscored warmup; an unfiltered journal cannot reconstruct that population.

Join sent, scored offers to terminal journal rows by `client_rid` to diagnose misses. Journal outcomes that represent misses include:

- invalid requests;
- capacity rejections;
- predictive refusals;
- expiries;
- engine failures;
- cancellations, including those with partial output and measured timing;
- terminal requests whose `output_len` is null;
- terminal requests whose `ttft_s` is null.

Completed responses above an applicable client latency limit also miss. Narwhal writes timing measured before a client disconnect to the `cancelled` terminal row and increments the cancellation counter for that original request. A non-null `output_len` or `ttft_s` does not turn that cancellation into a pass.

The router's [rolling `attainment` diagnostic](../http-api/06-SLO-and-Demand.md#slo-attainment) excludes cancelled, rejected, and invalid requests. It has a different population from deployment acceptance and cannot replace the client's all-offer score.

### Separate transfer and decode queueing

For requests whose first byte follows prefill, `first_byte_s - ttft_s` measures KV transfer plus decode queueing.

A crossed request normally pays both components. A request decoded on the local engine can still wait in the decode queue.

### Journal events

Narwhal appends router operation events to the request journal for:

- role-floor breaches and recoveries;
- blocked decode-floor changes;
- engine lifecycle operations;
- monitoring health.

Monitoring writes these event types:

- `monitoring_stage_failure`, including `stage`, `class`, and the stage-local `consecutive` failure count;
- `monitoring_degraded` when consecutive failures reach `controller.monitor_failure_limit`;
- `monitoring_recovered` after a fully successful monitoring pass clears degraded state.

Select terminal request rows and process event rows separately during journal analysis.

Failure text can contain engine IDs and engine URLs. Remove those identifiers before publishing timing journals.
