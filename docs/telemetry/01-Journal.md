# Request journal

## Diagnose a request from the journal

`--journal <path>` on `narwhal-serve` sets the JSON Lines journal path. Default: `journal.jsonl` beside `profiles.path`.

Each record carries a per-process `run` identifier, which separates restarts and standby takeovers in a shared file.

Terminal request rows carry `terminal`. Router event rows carry `event`.

The first row identifies the journal contract and the build that produced it:

```json
{"meta":{"schema":"narwhal.journal","schema_version":1,"package":"narwhal-inference","version":"0.1.0","git":"<commit>","source":"sha256:...","token_accounting":"token_ids"}}
```

`source` is the SHA-256 digest of the installed package's Python files. Identical source gives the same digest wherever it is installed.

### Terminal request records

The router writes one terminal row per original completion request, including its retries.

| Field                                        | Meaning                                                                                                                                                               |
| -------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `run`, `rid`, `client_rid`                   | Router process, Narwhal request ID, and optional caller request ID.                                                                                                   |
| `arrived`                                    | Arrival time on the process monotonic clock. Comparable within one `run`.                                                                                |
| `input_len`, `output_len`, `wanted_len`      | Prompt tokens, returned tokens, and requested output tokens. For a cancellation, `output_len` records delivered tokens when the router can measure them.              |
| `ttft_s`, `tpot_s`, `first_byte_s`           | Router-side prefill, decode, and first-visible-output timing.                                                                                                         |
| `prefill_iid`, `decode_iid`                  | Engines selected for the prefill and decode legs.                                                                                                                     |
| `crossed`                                    | Whether decode consumed KV produced by the recorded prefill engine.                                                                                                   |
| `token_accounting`                           | Decode-output accounting mode: `token_ids` for exact per-token identity, or `unavailable` when the engine dialect omits token IDs.                                     |
| `refused`, `refused_cause`                   | Predictive refusal flag and reason; see [refusal causes](#refusal-causes).                                                                                            |
| `cancelled`, `cancelled_phase`               | Client disconnect and the phase in which it occurred: `admission`, `queue`, `backoff`, `prefill`, or `decode`.                                                        |
| `terminal`                                   | Final request state: `completed`, `failed`, `refused`, `rejected`, `expired`, `invalid`, or `cancelled`.                                                              |
| `input_sized`                                | Whether local input sizing completed. When false, the body ended before sizing and the row records `input_len: 0`.                                                    |
| `attempts`, `decode_attempts`                | Prefill and decode dispatch counts for the original request.                                                                                                          |
| `attempt_failures`                           | Bounded records for failed attempts, including failures followed by successful retries.                                                                               |
| `queue_wait_s`, `duration_s`                 | Total admission and dispatch wait, and complete request lifetime on the router clock.                                                                                 |
| `decode_tpot_s`                              | Time from first to last observed output token divided by `output_tokens - 1`. Null when the router observed fewer than two tokens or exact token accounting is unavailable. |
| `decode_tokens_observed`, `upstream_seconds` | Tokens seen across all attempts, and the summed duration of every HTTP leg. Both include failed work.                                                                |
| `error`                                      | Failure or refusal detail. Successful and cancelled requests use null.                                                                                                |

#### Refusal causes

- `prompt`: the prompt alone exceeds the time to first token (TTFT) budget.
- `queue`: the prompt fits the TTFT budget, but the total predicted TTFT with queueing exceeds it.
- `aggregate_unpriced`: every candidate engine carries decode work. Aggregate prefill pricing requires an engine with zero resident decode work.

The [global admission policy](../configuration/02-Serving-and-Role-Control.md#41-global-admission) sets the budgets.

#### Attempt failures

Each `attempt_failures` entry records the monotonic time, attempt number, request phase, prefill and decode engine IDs, exception type, message and status, transient classification, visible-output state, retry decision and scheduled backoff.

Narwhal keeps at most `serving.max_attempts` entries. Messages are cut at 240 characters. The retry decision is the scheduled action. A cancellation during backoff interrupts it.

Remove engine IDs and engine URLs from failure text before publishing timing journals.

### Separate transfer and decode queueing

For requests whose first byte follows prefill, `first_byte_s - ttft_s` measures KV transfer plus decode queueing.

A crossed request includes both. A locally decoded request includes only the decode wait.

## Attainment accounting

Score every [scheduled client offer](../measure/04-Reconcile-and-Accept.md#10-join-client-offers-to-the-router-journal) against the client's TTFT and time per output token (TPOT) limits. Unsent offers count. The unscored warmup does not.

An offer passes when the client received a completed response within the applicable limits. Every other scored offer is a miss.

Join sent, scored offers to terminal journal rows by `client_rid`. Invalid requests, capacity rejections, predictive refusals, expiries, engine failures and cancellations all miss. So does any terminal row with a null `output_len` or `ttft_s`.

The `cancelled` terminal row keeps timing measured before the client disconnect. The cancellation counter increments for that original request.

The router's [rolling `attainment` diagnostic](../http-api/06-SLO-and-Demand.md#slo-attainment) skips cancelled, rejected and invalid requests, so it reads higher than the client's all-offer score. Accept deployments on the client score.

## Journal events

The router appends event rows for role-floor breaches and recoveries, blocked decode-floor changes, engine lifecycle operations and monitoring health.

A failed profile-generation check during a health or inference recovery probe blocks the engine from placement. Narwhal writes an `engine_lifecycle` event with `action: profile_recovery_blocked`.

| Field   | Meaning                  |
| ------- | ------------------------ |
| `iid`   | Engine.                  |
| `error` | Failed check.            |
| `at`    | Unix wall-clock seconds. |

Inspect the engine's [profile generation evidence](02-Profiles.md#validate-the-engine-cost-model) before retrying recovery.

Engine monitoring event types:

- `monitoring_stage_failure`, including `stage`, `class`, and the stage-local `consecutive` failure count;
- `monitoring_degraded` when consecutive failed monitoring passes reach `controller.monitor_failure_limit`;
- `monitoring_recovered` after a fully successful monitoring pass clears degraded state.
