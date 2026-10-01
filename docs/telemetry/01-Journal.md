---
description: Diagnose requests and SLO attainment from the Narwhal JSON Lines request journal.
---

# Request journal

## Diagnosing a request from the journal

`--journal <path>` on `narwhal-serve` sets the JSON Lines journal path, `journal.jsonl` beside `profiles.path` by default.

| Row type | Key field | Written |
| --- | --- | --- |
| Build metadata | `meta` | Once per router process, at journal open |
| Terminal request | `terminal` | Once per original request |
| Router event | `event` | Once per event |

Terminal and event rows carry these fields:

| Field | Value |
| --- | --- |
| `schema` | `narwhal.journal` |
| `schema_version` | `1` |
| `run` | Router process identifier, new on each restart or standby takeover |

`/narwhal/state` reports the current process's `run` as `journal_run`.

Build metadata row:

```json
{"meta":{"schema":"narwhal.journal","schema_version":1,"package":"narwhal-inference","version":"0.3.1","git":"<commit>","source":"sha256:...","token_accounting":"token_ids"}}
```

| Field | Meaning |
| --- | --- |
| `version` | Installed `narwhal-inference` version. |
| `git` | `git describe` output for a source checkout, null for an installed package. |
| `source` | SHA-256 digest of the installed package's Python files. |
| `token_accounting` | Decode-output accounting mode of the fleet's engine dialect. |

### Terminal request records

Each original completion request and its retries share one terminal row.

| Field | Meaning |
| --- | --- |
| `run`, `rid`, `client_rid` | Router process, Narwhal request ID, and the caller's `X-Request-Id` header. |
| `arrived` | Arrival time on the monotonic clock of the `run`'s router process. |
| `input_len` | Prompt tokens. |
| `input_sized` | Whether local input sizing completed. |
| `wanted_len` | Requested output tokens. |
| `output_len` | Output tokens the router measured up to completion or cancellation. |
| `ttft_s` | Seconds from arrival to prefill completion. |
| `tpot_s` | Mean seconds per output token after prefill completion. |
| `first_byte_s` | Seconds from arrival to the first visible output. |
| `decode_tpot_s` | Time from first to last observed output token divided by `output_len - 1`. |
| `prefill_iid`, `decode_iid` | Engines of the final attempt's prefill and decode legs. |
| `crossed` | `true` when `decode_iid` differs from `prefill_iid`. |
| `cached_tokens` | Prompt tokens each listed engine serves from its prefix cache at the last prefill-placement recheck, by engine ID. |
| `cache_placement` | The prefill placement priced with [cache evidence](#cache-placement). |
| `token_accounting` | `token_ids` for exact per-token identity, `unavailable` when the engine dialect omits token IDs. |
| `refused`, `refused_cause` | `true` on a predictive refusal, with its [refusal cause](#refusal-causes). |
| `rejected` | `true` on a capacity rejection. |
| `expired` | `true` on an admission or total deadline expiry. |
| `cancelled`, `cancelled_phase` | `true` on a client disconnect, with its phase: `admission`, `queue`, `backoff`, `prefill`, or `decode`. |
| `terminal` | Final request state: `completed`, `failed`, `refused`, `rejected`, `expired`, `invalid`, or `cancelled`. |
| `attempts`, `decode_attempts` | Prefill and decode dispatch counts for the original request. |
| `attempt_failures` | One [entry](#attempt-failures) per failed attempt, retried or final. |
| `queue_wait_s` | Total admission and dispatch wait. |
| `duration_s` | Complete request lifetime on the router clock. |
| `decode_tokens_observed` | Decode tokens read across every attempt. |
| `upstream_seconds` | Summed HTTP leg seconds per phase (`prefill`, `decode`) across every attempt, failed or successful. |
| `error` | Error detail on `failed`, `refused`, `rejected`, `expired`, and `invalid` rows. |

| Condition | Field values |
| --- | --- |
| `input_sized` is false | `input_len` is `0`. |
| `token_accounting` is `unavailable` | `output_len`, `tpot_s`, `decode_tpot_s`, and `decode_tokens_observed` are null. |
| Fewer than two measured output tokens | `tpot_s` and `decode_tpot_s` are null. |
| Prefill incomplete | `ttft_s` and `tpot_s` are null. |
| Zero visible output | `first_byte_s` is null. |
| `terminal` is `completed` or `cancelled` | `error` is null. |
| Input sizing finds an engine holding at least one leading prompt block before the final prompt token | `cached_tokens` lists that engine. |
| A prefill-placement recheck finds zero matching blocks on a listed engine | `cached_tokens` drops that engine. |
| The request sets `truncate_prompt_tokens`, `documents`, or `reasoning_effort` | `cached_tokens` is empty. |
| A chat message carries a multimodal content part | `cached_tokens` is empty. |
| `engine_contract.speculative_config` names a speculative-decoding setup | `cached_tokens` is empty. |
| The fleet leaves `engine_contract` unset | `cached_tokens` is empty. |
| Input sizing uses the local estimate or a count-only tokenization response | `cached_tokens` is empty. |
| `cached_tokens` is empty | `cache_placement` is null. |
| The placed engine is outside the profile store | `cache_placement` is null. |

#### Cache placement

| Field | Meaning |
| --- | --- |
| `placed_iid` | Engine chosen for the prefill leg. |
| `placed_cached_tokens` | The chosen engine's `cached_tokens` entry. |
| `evidence_sequence` | Sidecar residency sequence behind the cached count. |
| `predicted_prefill_s` | Prefill seconds priced on the chosen engine. |
| `cold_prefill_s` | Cold prefill seconds for the full input on the chosen engine. |
| `cold_choice_iid` | Engine that cold pricing chooses among the same placement candidates. |

#### Refusal causes

The [global admission policy](../configuration/02-Serving-and-Role-Control.md#41-global-admission) sets the time to first token (TTFT) budget.

| `refused_cause` | Condition |
| --- | --- |
| `prompt` | The prompt's prefill alone exceeds the TTFT budget. |
| `queue` | The prompt alone fits the TTFT budget, and the cheapest placement including queueing exceeds it. |
| `aggregate_unpriced` | Every candidate engine carries decode work. |
| `decode` | Peak projected decode work over the request's decode window exceeds live decode capacity, or decode load pushes the request past `slo.tpot_s`. |

#### Attempt failures

| Field | Meaning |
| --- | --- |
| `at` | Monotonic time of the failure. |
| `attempt` | Attempt number. |
| `phase` | Request phase. |
| `prefill_iid`, `decode_iid` | Engines of the failed attempt. |
| `error_type`, `error_message`, `status` | Exception type, message cut at 240 characters, and status. |
| `transient` | Transient classification. |
| `output_started` | Whether client-visible output had started. |
| `retry_scheduled` | Whether a retry was scheduled. |
| `retry_reason` | `allowed`, `output_started`, `attempt_limit`, `non_transient`, `original_deadline`, or `shared_budget`. |
| `backoff_s` | Scheduled backoff seconds. |

| Property | Value |
| --- | --- |
| Entry limit | At most `serving.max_attempts`. |
| Cancellation during backoff | The last entry keeps `retry_scheduled: true`. |

Remove engine IDs and engine URLs from failure text before publishing timing journals.

### Separating transfer and decode queueing

For requests whose first byte follows prefill:

| Request | `first_byte_s - ttft_s` contains |
| --- | --- |
| Crossed | KV transfer plus decode queueing. |
| Locally decoded | Decode queueing. |

## Attainment accounting

Score every [scheduled client offer](../measure/04-Reconcile-and-Accept.md#10-joining-client-offers-to-the-router-journal) after the unscored warmup, sent or unsent, against the client's TTFT and time per output token (TPOT) limits.

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

| `event` | Written when | Fields beside `at` |
| --- | --- | --- |
| `below_floor` | Live prefill engines fall below `min_prefill`. | `live_prefill`, `min_prefill`, `ejected`, `quarantined` |
| `below_floor_recovered` | The live prefill pool returns to `min_prefill`. | `duration_s`, `live_prefill`, `min_prefill` |
| `controller_decision` | The role controller records an `applied`, `blocked`, `held`, or `advisory` decision. | `prefill`, `decode`, `by`, `reason`, `result`, `applied`, decision details |
| `decode_floor_restored` | A role change restores `min_decode`. | `iid`, `live_decode`, `min_decode` |
| `engine_lifecycle` | An engine lifecycle operation runs. | `action` and its operation fields |
| `monitoring_stage_failure` | A monitoring stage fails. | `stage`, `class`, stage-local `consecutive` count |
| `monitoring_degraded` | Consecutive failed monitoring passes reach `controller.monitor_failure_limit`. | `stage`, `class`, `core_consecutive` |
| `monitoring_recovered` | A fully successful monitoring pass clears degraded state. | |

| Event | `at` clock |
| --- | --- |
| `engine_lifecycle` | Unix wall-clock seconds |
| Every other event | Router monotonic clock |

A failed profile-generation check during a health or inference recovery probe:

- ejects the engine
- writes an `engine_lifecycle` event with `action: profile_recovery_blocked`

| Field | Meaning |
| --- | --- |
| `iid` | Engine. |
| `error` | Failed checks. |
| `at` | Unix wall-clock seconds. |

Inspect the engine's [profile generation evidence](02-Profiles.md#validating-the-engine-cost-model) before retrying recovery.
