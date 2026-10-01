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

`serving.handoff_timeout_s` counts handoff age from the start of the producer HTTP leg.

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

A timeout-shaped engine fault maps to HTTP `504`, and every other engine fault maps to HTTP `502`.

Prefill failures, non-streaming decode failures, and streaming decode failures before the first output return that HTTP error status. A streaming decode failure after the first output ends the HTTP `200` stream with a terminal server-sent event (SSE):

```text
data: {"error": ...}
```

The client error body carries a generic message, such as `Upstream request failed`. The engine failure detail goes to the `error` field of the [terminal request record](../telemetry/01-Journal.md#terminal-request-records).

### Input sizing

The router sizes each request's input before placement:

| Input                                                                              | Sizing before placement    |
| ---------------------------------------------------------------------------------- | -------------------------- |
| Nonempty `prompt` array of nonnegative integer token IDs                           | Local ID count             |
| Text or chat, with `engine.tokenize` on and an exact-count endpoint in the dialect | Exact count from an engine |
| Other input                                                                        | Character ratio            |

A failed exact count puts that engine into count backoff for 1 second. Each further consecutive failure doubles the backoff, up to 30 seconds, and a successful count resets it.

The exact count goes to the live engine outside count backoff that holds the fewest requests. When every live engine is in count backoff, the exact count goes to the live engine that holds the fewest requests.

Tokenization failures return the [engine-fault mapping](#engine-failure-handling) status before placement.

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

`serving.max_attempts` sets the prefill/decode attempts per admitted request, from `1` (default) to `3`.

A fresh attempt starts for a transient fault before visible output when:

- the original request deadline permits it
- retry budget remains

Each retry receives:

- fresh backend request IDs
- a new KV handoff

With `recovery.failure_quarantine_s` above `0`, placement of the engine whose leg failed follows [failure quarantine](../concepts/03-Failure-and-State.md#failure-quarantine).
