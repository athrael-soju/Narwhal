# Request journal

## Diagnose a request from the journal

`--journal <path>` on `narwhal-serve` sets the JSON Lines journal path, `journal.jsonl` beside `profiles.path` by default.

Every record carries a per-process `run` identifier that changes on each restart or standby takeover.

| Row type          | Key field  |
| ----------------- | ---------- |
| Terminal request  | `terminal` |
| Router event      | `event`    |

The first row records the journal schema and the build:

```json
{"meta":{"schema":"narwhal.journal","schema_version":1,"package":"narwhal-inference","version":"0.1.0","git":"<commit>","source":"sha256:...","token_accounting":"token_ids"}}
```

`source` is the SHA-256 digest of the installed package's Python files.

### Terminal request records

Each original completion request and its retries share one terminal row.

| Field                                        | Meaning                                                                                                                                                               |
| -------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `run`, `rid`, `client_rid`                   | Router process, Narwhal request ID, and optional caller request ID.                                                                                                   |
| `arrived`                                    | Arrival time on the process monotonic clock, comparable within one `run`.                                                                                             |
| `input_len`, `output_len`, `wanted_len`      | Prompt tokens, returned tokens, and requested output tokens, with `output_len` on a cancellation counting the delivered tokens the router measured.                |
| `ttft_s`, `tpot_s`, `first_byte_s`           | Router-side prefill, decode, and first-visible-output timing.                                                                                                         |
| `prefill_iid`, `decode_iid`                  | Engines selected for the prefill and decode legs.                                                                                                                     |
| `crossed`                                    | Whether decode consumed KV produced by the recorded prefill engine.                                                                                                   |
| `token_accounting`                           | Decode-output accounting mode: `token_ids` for exact per-token identity, or `unavailable` when the engine dialect omits token IDs.                                     |
| `refused`, `refused_cause`                   | Predictive refusal flag and [refusal cause](#refusal-causes).                                                                                                         |
| `cancelled`, `cancelled_phase`               | Client disconnect and the phase in which it occurred: `admission`, `queue`, `backoff`, `prefill`, or `decode`.                                                        |
| `terminal`                                   | Final request state: `completed`, `failed`, `refused`, `rejected`, `expired`, `invalid`, or `cancelled`.                                                              |
| `input_sized`                                | Whether local input sizing completed, with `input_len: 0` when false.                                                                                                 |
| `attempts`, `decode_attempts`                | Prefill and decode dispatch counts for the original request.                                                                                                          |
| `attempt_failures`                           | Bounded records of every failed attempt, retried or final.                                                                                                            |
| `queue_wait_s`, `duration_s`                 | Total admission and dispatch wait, and complete request lifetime on the router clock.                                                                                 |
| `decode_tpot_s`                              | Time from first to last observed output token divided by `output_tokens - 1`, or null when fewer than two tokens arrived or `token_accounting` is `unavailable`.       |
| `decode_tokens_observed`, `upstream_seconds` | Tokens seen and summed HTTP leg duration across every attempt, failed or successful.                                                                                  |
| `error`                                      | Failure or refusal detail, or null for successful and cancelled requests.                                                                                             |

#### Refusal causes

The [global admission policy](../configuration/02-Serving-and-Role-Control.md#41-global-admission) sets the time to first token (TTFT) budgets.

- `prompt`: the prompt alone exceeds the TTFT budget.
- `queue`: the prompt fits the TTFT budget and the predicted TTFT including queueing exceeds it.
- `aggregate_unpriced`: every candidate engine carries decode work.

#### Attempt failures

Each `attempt_failures` entry records:

- monotonic time
- attempt number
- request phase
- prefill and decode engine IDs
- exception type, message, and status
- transient classification
- visible-output state
- retry decision
- scheduled backoff

| Property          | Value                                                                  |
| ----------------- | ---------------------------------------------------------------------- |
| Entry limit       | At most `serving.max_attempts`.                                        |
| Message length    | Cut at 240 characters.                                                 |
| Retry decision    | The scheduled action, which a cancellation during backoff interrupts.  |

Remove engine IDs and engine URLs from failure text before publishing timing journals.

### Separate transfer and decode queueing

For requests whose first byte follows prefill:

| Request          | `first_byte_s - ttft_s` contains    |
| ---------------- | ----------------------------------- |
| Crossed          | KV transfer plus decode queueing.   |
| Locally decoded  | Decode queueing.                    |

## Attainment accounting

Score every [scheduled client offer](../measure/04-Reconcile-and-Accept.md#10-join-client-offers-to-the-router-journal) after the unscored warmup, sent or unsent, against the client's TTFT and time per output token (TPOT) limits.

An offer passes when the client received a completed response within the applicable limits.

Joined to sent, scored offers by `client_rid`, these terminal journal rows are misses:

- invalid requests
- capacity rejections
- predictive refusals
- expiries
- engine failures
- cancellations
- a row with a null `output_len` or `ttft_s`

For a cancelled request:

- the terminal row keeps timing measured before the client disconnect
- the cancellation counter increments for the original request

The router's [rolling `attainment` diagnostic](../http-api/06-SLO-and-Demand.md#slo-attainment) skips cancelled, rejected, and invalid requests.

Accept deployments on the client's all-offer score.

## Journal events

Event rows record:

- role-floor breaches and recoveries
- blocked decode-floor changes
- engine lifecycle operations
- monitoring health

A failed profile-generation check during a health or inference recovery probe:

- blocks the engine from placement
- writes an `engine_lifecycle` event with `action: profile_recovery_blocked`

| Field   | Meaning                  |
| ------- | ------------------------ |
| `iid`   | Engine.                  |
| `error` | Failed check.            |
| `at`    | Unix wall-clock seconds. |

Inspect the engine's [profile generation evidence](02-Profiles.md#validate-the-engine-cost-model) before retrying recovery.

Engine monitoring event types:

- `monitoring_stage_failure`: a failed monitoring stage, with `stage`, `class`, and the stage-local `consecutive` failure count.
- `monitoring_degraded`: consecutive failed monitoring passes reached `controller.monitor_failure_limit`.
- `monitoring_recovered`: a fully successful monitoring pass cleared degraded state.
