---
description: Disaggregated prefill and decode execution and engine failure handling in the Narwhal router.
---

# Backend execution and failures

## Disaggregated backend execution

### Prefill

The prefill leg is a non-streaming, one-token completion request to the producer.

### Decode

Remote decode receives:

- the original prompt or messages
- the requested output limit
- sampling settings
- the validated handoff descriptor

Same-engine decode:

- strips `kv_transfer_params` from the request
- uses engine prefix caching or prompt recomputation

### Descriptor validation

Run [preflight](../deploy/06-Profile-and-Preflight.md#running-preflight) to validate the pinned engine contract and role-permitted KV transfers.

Decode rejects a KV handoff descriptor with:

- missing engine identity
- malformed block IDs
- a connector mismatch
- an endpoint mismatch

### Request scope and timing

Scoped to the original client request:

- handoff-age enforcement
- phase reservations
- retries
- cleanup

Handoff age counts from prefill completion, when the producer's lease starts.

### KV handoff expiry

The [KV handoff bound](../configuration/02-Serving-and-Role-Control.md#kv-handoff-bound) limits the time from prefill completion to decode dispatch. A handoff that reaches the bound ends its attempt:

| Point of expiry | Journal `error` contains |
| --- | --- |
| Before decode dispatch | `KV handoff expired before decode dispatch` |
| While waiting for a decode seat, with `serving.queue_capacity` above 0 | `KV handoff bound reached while waiting for a decode seat` |

The expiry is transient, so a request with attempts remaining retries with a fresh prefill. When the request has no attempts left, the router returns HTTP `504` with error type and code `handoff_expired` and the message `KV handoff expired before decode dispatch`. The journal records terminal `expired` with reason `handoff`.

Durations in the [measurement contract](../measure/01-Profile.md):

| Duration                | Interval                                                           |
| ----------------------- | ------------------------------------------------------------------ |
| `ttft_s`                | Request arrival at the router to producer HTTP completion          |
| `first_byte_s`          | Request arrival at the router to the first generated decode output |
| `first_byte_s - ttft_s` | Producer completion to the first decode output                     |

### Python API

```python
from narwhal.engines.client import EngineClient
from narwhal.engines.connector import PrefillResult
```

| Object                   | Contract                                                                        |
| ------------------------ | ------------------------------------------------------------------------------- |
| `EngineClient.prefill()` | Returns a `PrefillResult` to pass to `EngineClient.decode()`                    |
| `PrefillResult`          | Holds the KV handoff descriptor, producer URL, endpoint, and backend request ID |
| `result.parameters()`    | Returns a detached dictionary                                                   |

## Engine failure handling

A timeout-shaped engine fault maps to HTTP `504`, and every other engine fault maps to HTTP `502`. A timeout-shaped fault is an engine HTTP `408` or `504` status, or a connect, connection-pool, prefill, first-token, between-token, or exact-count timeout.

Exact-count failures, prefill failures, non-streaming decode failures, and streaming decode failures before the first output return that HTTP error status when the request does not [retry](#retries). The error `type` and `code` both name the request phase in which the fault ended the request:

| Condition | HTTP | Error `type` | Error `code` |
| --- | :---: | --- | --- |
| The last [exact input count](#input-sizing) fails, or a count fails non-transiently, with a timeout-shaped fault | `504` | `admission` | `admission` |
| The last exact input count fails, or a count fails non-transiently, with any other fault | `502` | `admission` | `admission` |
| The prefill leg ends with a timeout-shaped fault and the request does not retry | `504` | `prefill` | `prefill` |
| The prefill leg ends with any other fault and the request does not retry | `502` | `prefill` | `prefill` |
| The decode leg ends with a timeout-shaped fault before output starts and the request does not retry | `504` | `decode` | `decode` |
| The decode leg ends with any other fault before output starts and the request does not retry, or a non-streaming response fails assembly or exceeds `serving.max_response_bytes` | `502` | `decode` | `decode` |

The client error body carries a generic message, such as `Upstream request failed`. The engine failure detail goes to the `error` field of the [terminal request record](../telemetry/01-Journal.md#terminal-request-records).

A streaming decode failure after the first output ends the HTTP `200` stream with a terminal server-sent event (SSE). Its error `type` is the request phase, `decode`, and its `code` is the terminal state, `failed` or `expired`:

```text
data: {"error": {"message": "Upstream request failed", "type": "decode", "code": "failed"}}
```

An unexpected router error returns HTTP `500` with a plain-text body and no error `type`. The journal records terminal `failed` with reason `internal`.

### Input sizing

The router sizes each request's input before placement:

| Input                                                                              | Sizing before placement    |
| ---------------------------------------------------------------------------------- | -------------------------- |
| Nonempty `prompt` array of nonnegative integer token IDs                           | Local ID count             |
| Text or chat, with `engine.tokenize` on and an exact-count endpoint in the dialect | Exact count from an engine |
| Other input                                                                        | Character ratio            |

A failed exact count puts that engine into count backoff for 1 second. Each further consecutive failure doubles the backoff, up to 30 seconds, and a successful count resets it. The failure also counts as [failure evidence](../concepts/03-Failure-and-State.md#failure-evidence) against that engine.

After a transient failure, the router moves the exact count to another live engine that has not failed it for this request. A request tries at most `serving.max_attempts` engines for its exact count, and these tries spend no retry credit. A non-transient failure, such as an HTTP 400 for the prompt, ends the count on its first engine.

The exact count goes to the live engine outside count backoff that holds the fewest requests. When every live engine is in count backoff, the exact count goes to the live engine that holds the fewest requests.

When the last exact count fails, or a count fails non-transiently, the request returns the [engine-fault mapping](#engine-failure-handling) status before placement.

### Breaker ejection and readmission

[Failure evidence](../concepts/03-Failure-and-State.md#failure-evidence) gives each decode-leg failure's breaker class and the probe that decides ejection or readmission.

[Last-engine protection](../concepts/03-Failure-and-State.md#last-engine-protection) keeps an uncovered suspect in placement during its inference probe.

### Successful stream termination

A valid engine stream ends with `data: [DONE]` after generated output.

| Stream                                                                              | Result                            | Journal `error` contains                            |
| ----------------------------------------------------------------------------------- | --------------------------------- | --------------------------------------------------- |
| Closes before `[DONE]`                                                              | Engine failure                    | `stream ended before the [DONE] terminator`         |
| `[DONE]` before the first generated token                                           | HTTP `502`                        | `stream ended with [DONE] before any token arrived` |
| Upstream HTTP `200` carrying an error object with an integer `code` from 400 to 599 | Engine failure with status `code` | Engine error message                                |
| Upstream HTTP `200` carrying any other error object                                 | Engine failure with status `500`  | Engine error message                                |

### Decode timeouts

`engine.first_token_timeout_s` bounds the time from decode request start, across connection and response headers, to the first generated token. On expiry, the router returns HTTP `504` and the journal `error` contains `no first token within`.

`engine.decode_read_timeout_s` bounds the silence between any two transport chunks after the first token. On expiry, the router returns HTTP `504` and the journal `error` contains `engine went silent between tokens`. With `engine.decode_read_timeout_s: 0`, the original request deadline bounds the stream.

### Retries

`serving.max_attempts` sets the prefill/decode attempts per admitted request, from `1` to `3`. The default, `2`, allows one retry. [Retries and quarantine](../operate/07-Admission-Queue-and-Retry-Settings.md#retries-and-quarantine) gives criteria for changing it.

A fresh attempt starts for a transient fault before visible output when:

- the original request deadline permits it
- retry budget remains

A retry reserves one retry credit when it is scheduled and spends that credit when its prefill dispatches. A retry that ends before dispatch returns its credit.

Each retry receives:

- fresh backend request IDs
- a new KV handoff
- placement that avoids the engines that failed earlier attempts

#### Retry placement

A retry excludes, for each role, every engine whose leg failed earlier in the same request:

| Failed leg                         | Engines the retry excludes                     |
| ---------------------------------- | ---------------------------------------------- |
| Prefill                            | That prefill engine                            |
| Decode, before visible output      | That decode engine and the attempt's producer  |

A decode failure before visible output also excludes the producer because its KV handoff can cause the failure. The exclusion lasts for that request only.

When no live engine outside the exclusions can take a role's legs, the request ends with HTTP `503` and error type `backend_unavailable`. Under [aggregate fallback](../concepts/02-Role-Control.md#aggregate-fallback), an unpinned engine of the other role can still take them. The journal records reason `no_engine`, and the final attempt entry carries `retry_reason` `not_dispatched`.

In `predictive` mode, a retry skips the TTFT check and runs the decode admission check. [Retry pricing](../configuration/02-Serving-and-Role-Control.md#retry-pricing) gives the check a retry passes.

With `recovery.failure_quarantine_s` above `0`, placement of the engine whose leg failed follows [failure quarantine](../concepts/03-Failure-and-State.md#failure-quarantine).
