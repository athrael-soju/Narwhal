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
{"meta":{"schema":"narwhal.journal","schema_version":1,"package":"narwhal-inference","version":"<version>","git":"<commit>","source":"sha256:...","token_accounting":"token_ids","admission":{"mode":"predictive","margin":0.0}}}
```

| Field | Meaning |
| --- | --- |
| `version` | Installed `narwhal-inference` version. |
| `git` | `git describe` output for a source checkout, null for an installed package. |
| `source` | SHA-256 digest of the installed package's Python files. |
| `token_accounting` | Decode-output accounting mode of the fleet's engine dialect. |
| `admission` | `mode` from `serving.admission` and `margin` from `serving.admission_margin`. |

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
| `rejected` | `true` on a capacity or router-readiness rejection. |
| `expired` | `true` on an admission or total deadline expiry. |
| `cancelled`, `cancelled_phase` | `true` on a client disconnect, with its phase: `admission`, `queue`, `backoff`, `prefill`, or `decode`. |
| `terminal` | Final request state: `completed`, `failed`, `refused`, `rejected`, `expired`, `invalid`, or `cancelled`. |
| `reason` | [Outcome reason](#outcome-reasons) on `failed`, `refused`, `rejected`, and `expired` rows. |
| `status` | HTTP status of the router's response. |
| `error_type`, `error_code` | `type` and `code` of the client error body or terminal stream event. |
| `readiness_reason` | On a `not_ready` rejection, the reason `/ready` reports. |
| `attempts`, `decode_attempts` | Prefill and decode dispatch counts for the original request. |
| `attempt_failures` | One [entry](#attempt-failures) per failed attempt, retried or final. |
| `queue_waits` | Seconds waited at each stage: `admission`, `prefill` seat, and `decode` seat. |
| `queue_wait_s` | Sum of `queue_waits`. |
| `admission_price` | The latest [prefill admission price](#admission-price) and its parts. |
| `duration_s` | Complete request lifetime on the router clock. |
| `decode_tokens_observed` | Decode tokens read across every attempt. |
| `upstream_seconds` | Summed HTTP leg seconds per phase (`prefill`, `decode`) across every attempt, failed or successful. |
| `error` | Error detail on `failed`, `refused`, `rejected`, `expired`, and `invalid` rows. |

These conditions set a field to `0` or null:

| Condition | Field values |
| --- | --- |
| `input_sized` is false | `input_len` is `0`. |
| `token_accounting` is `unavailable` | `output_len`, `tpot_s`, `decode_tpot_s`, and `decode_tokens_observed` are null. |
| Fewer than two measured output tokens | `tpot_s` and `decode_tpot_s` are null. |
| Prefill incomplete | `ttft_s` and `tpot_s` are null. |
| Zero visible output | `first_byte_s` is null. |
| `terminal` is `completed` or `cancelled` | `error`, `error_type`, and `error_code` are null. |
| `terminal` is `completed`, `invalid`, or `cancelled` | `reason` is null. |
| A stream has sent output | `status` is `200`; a later error ends the stream with a terminal event. |
| Cancelled before output | `status` is null. |
| The client error body has no `code` | `error_code` is null. |
| The request never reached a stage | That stage's `queue_waits` entry is null. |
| The request ended before the router priced its prefill placement | `admission_price` is null. |

The `prefill` and `decode` seat waits apply with `serving.queue_capacity` above `0`.

`cached_tokens` lists each engine that input sizing finds holding at least one leading prompt block before the final prompt token. A prefill-placement recheck drops a listed engine when it finds zero matching blocks on it.

`cached_tokens` is empty when:

- the request sets `truncate_prompt_tokens`, `documents`, or `reasoning_effort`
- a chat message carries a multimodal content part
- `engine_contract.speculative_config` names a speculative-decoding setup
- the fleet leaves `engine_contract` unset
- input sizing uses the local estimate or a count-only tokenization response

#### Cache placement

`cache_placement` is null when `cached_tokens` is empty or the placed engine is outside the profile store. Otherwise it carries these fields:

| Field | Meaning |
| --- | --- |
| `placed_iid` | Engine chosen for the prefill leg. |
| `placed_cached_tokens` | The chosen engine's `cached_tokens` entry. |
| `evidence_sequence` | Sidecar residency sequence behind the cached count. |
| `predicted_prefill_s` | Prefill seconds priced on the chosen engine. |
| `cold_prefill_s` | Cold prefill seconds for the full input on the chosen engine. |
| `cold_choice_iid` | Engine that cold pricing chooses among the same placement candidates. |

#### Refusal causes

The [global admission policy](../configuration/02-Serving-and-Role-Control.md#global-admission) sets the time to first token (TTFT) budget.

| `refused_cause` | Condition |
| --- | --- |
| `prompt` | The prompt's prefill alone exceeds the TTFT budget. |
| `queue` | The prompt alone fits the TTFT budget, and the cheapest placement including queueing exceeds it. |
| `aggregate_unpriced` | Every candidate engine carries decode work. |
| `decode` | The request fails one of the three [decode admission checks](../configuration/02-Serving-and-Role-Control.md#decode-admission-check): slot wait, KV tokens or TPOT. `reason` names the failed check. |

#### Outcome reasons

`reason` takes these values. The router's [outcome counters](03-Metrics-and-Control.md#reading-outcome-reasons) carry the same values.

| `terminal` | `reason` | Condition |
| --- | --- | --- |
| `refused` | `queue` | `refused_cause` is `queue`. |
| `refused` | `prompt` | `refused_cause` is `prompt`. |
| `refused` | `aggregate_unpriced` | `refused_cause` is `aggregate_unpriced`. |
| `refused` | `slot_wait` | The projected TTFT plus the decode slot wait exceeds the TTFT budget. |
| `refused` | `kv_capacity` | Peak decode KV tokens during the request's decode hold exceed live decode capacity. |
| `refused` | `tpot` | Decode load pushes the request past `slo.tpot_s` on every live decode engine. |
| `rejected` | `inflight_limit` | Requests counted against the router [in-flight limit](../configuration/02-Serving-and-Role-Control.md#in-flight-limit) reach it plus `serving.queue_capacity`. |
| `rejected` | `saturated` | Router event-loop lag or request-sizing time reaches a quarter of `slo.ttft_s`. |
| `rejected` | `not_ready` | The router is not ready to serve, or a [hold](../configuration/02-Serving-and-Role-Control.md#queue-waits) ends a waiting or prefilled request; `readiness_reason` gives the cause. |
| `expired` | `deadline` | The original request deadline, `serving.request_timeout_s`, expires. |
| `expired` | `queue_timeout` | The admission-seat and prefill-seat waits reach `serving.queue_timeout_s` before the original deadline. |
| `expired` | `handoff` | The KV handoff reaches the producer's [handoff bound](../configuration/02-Serving-and-Role-Control.md#kv-handoff-bound) before decode dispatch. |
| `failed` | `no_engine` | Zero live engines can take the prefill or decode leg. |
| `failed` | `engine_unreachable` | The connection to the engine fails or times out. |
| `failed` | `engine_connection` | An established engine connection fails or breaks the HTTP protocol. |
| `failed` | `engine_timeout` | The engine returns HTTP `504`, or a prefill, first-token, or between-token timeout expires. |
| `failed` | `engine_overloaded` | The engine returns HTTP `408` or `429`. |
| `failed` | `engine_rejected` | The engine returns another HTTP `4xx` status. |
| `failed` | `engine_error` | The engine returns another error status, an error event, a stream without `[DONE]`, or output without valid token IDs. |
| `failed` | `local_pool` | The wait for a router data connection reaches `engine.pool_timeout_s`. |
| `failed` | `invalid_response` | An engine response fails validation, such as a malformed KV handoff descriptor or an unassemblable non-streaming response. |
| `failed` | `response_limit` | The response exceeds `serving.max_response_bytes` or the pre-output metadata limit. |
| `failed` | `internal` | The router raises an unexpected error. |
| any counted state | `unclassified` | Another HTTP status before dispatch, or a count restored from a state handoff without its reasons. |

#### Attempt failures

Each `attempt_failures` entry carries these fields:

| Field | Meaning |
| --- | --- |
| `at` | Monotonic time of the failure. |
| `attempt` | Attempt number. |
| `phase` | Request phase: `admission`, `queue`, `prefill`, or `decode`. |
| `reason` | [Outcome reason](#outcome-reasons) of the failure. |
| `prefill_iid`, `decode_iid` | Engines of the failed attempt. |
| `error_type`, `error_message`, `status` | Exception type, message cut at 240 characters, and status. |
| `transient` | Transient classification. |
| `output_started` | Whether client-visible output had started. |
| `retry_scheduled` | Whether a retry was scheduled. |
| `retry_reason` | `allowed`, `not_dispatched`, `output_started`, `attempt_limit`, `non_transient`, `original_deadline`, or `shared_budget`. |
| `backoff_s` | Scheduled backoff seconds. |

An attempt starts at prefill placement and dispatches with its prefill leg. A failure before dispatch, such as a predictive refusal or a placement failure, ends the request in the undispatched attempt with `retry_reason: not_dispatched`. After a backoff, the request returns to the `admission` phase for its next attempt.

`attempt_failures` holds at most `serving.max_attempts` entries. After a cancellation during backoff, the last entry keeps `retry_scheduled: true`.

#### Admission price

The router prices each attempt's prefill placement in both [admission modes](../configuration/02-Serving-and-Role-Control.md#global-admission). `predictive` mode refuses the request when `price_s` exceeds the TTFT budget, and `open` mode records the price without enforcing it. In `predictive` mode, the router also prices a first attempt on the cheapest live prefill engine while it [waits](../configuration/02-Serving-and-Role-Control.md#queue-waits) for an admission or prefill seat. A price taken during an admission-seat wait precedes sizing, so its `own_prefill_s` is a cold prefill of the estimated input length, without cache evidence. The row keeps the latest price.

| Field | Meaning |
| --- | --- |
| `attempt` | Attempt the router priced. |
| `backlog_s` | Resident prefill work and any probation penalty on the priced prefill engine. |
| `own_prefill_s` | The request's own prefill on that engine, priced with its cache evidence once the router has sized the request. |
| `elapsed_s` | Seconds from arrival to pricing. |
| `price_s` | Projected TTFT: `backlog_s + own_prefill_s + elapsed_s`. |

`backlog_s` and `price_s` are null when aggregate prefill has no calibrated price. `own_prefill_s` and `backlog_s` are null when the placed engine is outside the profile store.

Compare `price_s` with `ttft_s` on completed rows to check the price against the observed TTFT.

Remove engine IDs and engine URLs from failure text before publishing timing journals.

### Separating transfer and decode queueing

For a crossed request whose first byte follows prefill, `first_byte_s - ttft_s` contains KV transfer plus decode queueing. For a locally decoded request, it contains decode queueing.

## Attainment accounting

Score every [scheduled client offer](../measure/04-Reconcile-and-Accept.md#joining-client-offers-to-the-router-journal) after the unscored warmup, sent or unsent, against the client's TTFT and time per output token (TPOT) limits.

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

Each router event row carries an `at` timestamp, in Unix wall-clock seconds for `engine_lifecycle` and on the router monotonic clock for every other event.

| `event` | Written when | Fields beside `at` |
| --- | --- | --- |
| `below_floor` | Live prefill engines fall below `min_prefill`. | `live_prefill`, `min_prefill`, `ejected`, `quarantined` |
| `below_floor_recovered` | The live prefill pool returns to `min_prefill`. | `duration_s`, `live_prefill`, `min_prefill` |
| `controller_decision` | The role controller records an `applied`, `blocked`, `held`, or `advisory` decision. | `prefill`, `decode`, `by`, `reason`, `result`, `applied`, [decision details](../http-api/05-Live-State.md#controller-decisions) |
| `decode_floor_restored` | A role change restores `min_decode`. | `iid`, `live_decode`, `min_decode` |
| `engine_ejected` | The breaker or a recovery check ejects an engine. | `iid`, [`cause`](04-Failures.md#ejections-holds-and-readmissions) |
| `engine_hold_ended` | A timed quarantine or inference hold ends. | `iid`, `kind`, [`cause`](04-Failures.md#ejections-holds-and-readmissions), `held_s` |
| `engine_hold_started` | A timed quarantine or inference hold starts. | `iid`, [`kind`](04-Failures.md#hold-kinds), `duration_s` (`null` for an inference hold) |
| `engine_lifecycle` | An engine lifecycle operation runs. | `action` and its operation fields |
| `engine_probe` | A health or inference verification probe returns. | `iid`, `kind`, [`outcome`](04-Failures.md#verification-probe-outcomes); [inference probe fields](#inference-probe-events) |
| `engine_readmitted` | An ejected engine returns to placement. | `iid`, [`evidence`](04-Failures.md#ejections-holds-and-readmissions) |
| `monitoring_stage_failure` | A monitoring stage fails. | `stage`, `class`, stage-local `consecutive` count |
| `monitoring_degraded` | Consecutive failed monitoring passes reach `controller.monitor_failure_limit`. | `stage`, `class`, `core_consecutive` |
| `monitoring_recovered` | A fully successful monitoring pass clears degraded state. | |

### Inference probe events

An `engine_probe` event with `kind: verify_inference` adds these fields:

| Field | Meaning |
| --- | --- |
| `producer` | Engine that ran the probe's prefill leg, `null` for a standalone probe. |
| `recorded_producer` | Producer recorded by the failed transfer this probe verifies, `null` when none was recorded. |
| `producer_class` | Failure class of the producer leg, on a `producer_failed` outcome. |
| `prefill_class`, `decode_class` | Failure class of each failed leg, on a `failed` outcome. |

### Profile recovery events

A failed profile-generation check during a health or inference recovery probe:

- ejects the engine and writes an `engine_ejected` event with `cause: profile_generation`
- writes an `engine_lifecycle` event with `action: profile_recovery_blocked`

That event carries these fields:

| Field | Meaning |
| --- | --- |
| `iid` | Engine. |
| `error` | Failed checks. |
| `at` | Unix wall-clock seconds. |

Inspect the engine's [profile generation evidence](02-Profiles.md#validating-the-engine-cost-model) before retrying recovery.
