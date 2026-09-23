# Request journal

## Diagnose a request from the journal

Narwhal writes one JSON Lines journal when request journaling is enabled.

```bash
--journal <path>
```

When `--journal` is omitted, Narwhal writes `journal.jsonl` beside `profiles.path`. The file is append-only. Each router process writes its own `run` identifier, which forms the process boundary when records from multiple starts or standby takeovers share one file.

The first row identifies the journal contract and the build that produced it:

```json
{"meta":{"schema":"narwhal.journal","schema_version":1,"package":"narwhal-inference","version":"0.1.0","git":"<commit>","source":"sha256:...","token_accounting":"token_ids"}}
```

`source` is the SHA-256 digest of the installed package's Python files. Identical Python source produces the same digest from a checkout, deployment, or wheel.

### Terminal request records

Narwhal writes one terminal row for every original completion request. Invalid requests, capacity refusals, queue expiries, engine failures, client cancellations, and successful completions therefore share the same request-level record model. Retries remain attached to the original row rather than becoming separate terminal requests.

| Field                                        | Meaning                                                                                                                                                               |
| -------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `run`, `rid`, `client_rid`                   | Router process, Narwhal request ID, and optional caller request ID.                                                                                                   |
| `arrived`                                    | Arrival time on the process monotonic clock. Compare this value only within one `run`.                                                                                |
| `input_len`, `output_len`, `wanted_len`      | Prompt tokens, returned tokens, and requested output tokens. For a cancellation, `output_len` records delivered tokens when they can be measured.                     |
| `ttft_s`, `tpot_s`, `first_byte_s`           | Router-side prefill, decode, and first-visible-output timing.                                                                                                         |
| `prefill_iid`, `decode_iid`                  | Engines selected for the prefill and decode legs.                                                                                                                     |
| `crossed`                                    | Whether decode consumed KV produced by the recorded prefill engine.                                                                                                   |
| `token_accounting`                           | Decode-output accounting mode. `token_ids` provides exact per-token identity; every other dialect reports `unavailable`.                                              |
| `refused`, `refused_cause`                   | Predictive refusal state and the priced cause.                                                                                                                        |
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

Narwhal caps the list at `serving.max_attempts` and truncates messages to 240 characters. A recorded retry decision describes the scheduled action. The client may cancel during backoff before that retry reaches dispatch.

### Attainment accounting

Narwhal excludes predictive refusals and cancellations from journal attainment scoring.

The following terminal outcomes count as misses:

- invalid requests;
- capacity rejections;
- expiries;
- engine failures;
- terminal requests whose `output_len` is null;
- terminal requests whose `ttft_s` is null.

Cancelled rows retain any timing measured before disconnect and use `error: null`. Completion counters also exclude cancellations. Refusals, cancellations, and failures retain their own outcome categories rather than being collapsed into completion status.

### Separate transfer and decode queueing

For requests whose first byte follows completion of prefill:

```text
first_byte_s - ttft_s
```

measures KV transfer plus decode queueing.

A crossed request normally pays both components. A request decoded on the local engine can still wait in the decode queue.

### Journal events

The same JSONL stream carries event rows for router operations. Current event classes cover:

- role-floor breaches and recoveries;
- blocked decode-floor changes;
- engine lifecycle operations;
- monitoring health.

Monitoring writes these event types:

- `monitoring_stage_failure`, including `stage`, `class`, and the stage-local `consecutive` failure count;
- `monitoring_degraded` when consecutive failures reach `controller.monitor_failure_limit`;
- `monitoring_recovered` after a fully successful monitoring pass clears degraded state.

Request analysis should select terminal request rows and process event rows separately.

Failure text can contain engine IDs and engine URLs. Remove those identifiers before publishing timing journals.
