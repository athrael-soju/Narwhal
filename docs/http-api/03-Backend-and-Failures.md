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

Run [preflight](../deploy/06-Profile-and-Preflight.md#run-preflight) to validate the pinned engine contract and role-permitted KV transfers.

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

| Engine fault   | HTTP  |
| -------------- | :---: |
| Timeout-shaped | `504` |
| Other          | `502` |

| Failure                                             | Client response                                     |
| --------------------------------------------------- | --------------------------------------------------- |
| Prefill, streaming or non-streaming                 | HTTP error status                                   |
| Decode, non-streaming                               | HTTP error status                                   |
| Decode, streaming, before the first output          | HTTP error status                                   |
| Decode, streaming, after the first output           | A terminal server-sent event (SSE) after HTTP `200` |

```text
data: {"error": ...}
```

| Destination                                                                                         | Content                                            |
| --------------------------------------------------------------------------------------------------- | -------------------------------------------------- |
| Client error body                                                                                   | Generic message, such as `Upstream request failed` |
| `error` field of the [terminal request record](../telemetry/01-Journal.md#terminal-request-records) | Engine failure detail                              |

### Input sizing

| Input                                                                              | Sizing before placement    |
| ---------------------------------------------------------------------------------- | -------------------------- |
| Nonempty `prompt` array of nonnegative integer token IDs                           | Local ID count             |
| Text or chat, with `engine.tokenize` on and an exact-count endpoint in the dialect | Exact count from an engine |
| Other input                                                                        | Character ratio            |

Count backoff per engine:

| Count result                     | Count backoff             |
| -------------------------------- | ------------------------- |
| First failure                    | 1 second                  |
| Each further consecutive failure | Doubles, up to 30 seconds |
| Success                          | Reset                     |

Exact-count engine selection:

| Live engines outside count backoff | Exact count goes to                                             |
| ---------------------------------- | --------------------------------------------------------------- |
| One or more                        | The engine outside count backoff that holds the fewest requests |
| Zero                               | The live engine that holds the fewest requests                  |

Tokenization failures return the [engine-fault mapping](#engine-failure-handling) status before placement.

### Breaker ejection and readmission

| Event                                                                                                | Breaker action                             |
| ---------------------------------------------------------------------------------------------------- | ------------------------------------------ |
| `recovery.eject_after` consecutive stream failures                                                   | Starts an inference probe                  |
| Failed inference probe while every role the engine places stays placeable through other live engines | Ejects the engine                          |
| Other failed inference probe                                                                         | Keeps the engine in placement              |
| Successful inference probe                                                                           | Readmits the engine                        |

Decode-leg failures by breaker class:

| Failure                                                 | Class      | Probe at `recovery.eject_after` consecutive failures |
| ------------------------------------------------------- | ---------- | ---------------------------------------------------- |
| First-token timeout while the engine emits other output | `overload` | Health probe                                         |
| First-token timeout from a silent engine                | `stream`   | Inference probe                                      |
| Mid-stream silence                                      | `stream`   | Inference probe                                      |
| Stream closed before `[DONE]`                           | `stream`   | Inference probe                                      |
| `[DONE]` before the first token                         | `stream`   | Inference probe                                      |

Inference-probe suspect placement:

| Condition                                                                                                | Placement during the probe |
| -------------------------------------------------------------------------------------------------------- | -------------------------- |
| Every role the suspect places stays placeable through other live engines                                 | Held out                   |
| Another engine's ejection or drain leaves zero other live engines for a role the held-out suspect places | Returned to placement      |
| Any other case                                                                                           | Kept in placement          |

Inference probes apply the larger of `engine.first_token_timeout_s` and `engine.health_timeout_s` to each leg.

### Successful stream termination

A valid engine stream ends with `data: [DONE]` after generated output.

| Stream                                                                              | Result                            | Journal `error` contains                            |
| ----------------------------------------------------------------------------------- | --------------------------------- | --------------------------------------------------- |
| Closes before `[DONE]`                                                              | Engine failure                    | `stream ended before the [DONE] terminator`         |
| `[DONE]` before the first generated token                                           | HTTP `502`                        | `stream ended with [DONE] before any token arrived` |
| Upstream HTTP `200` carrying an error object with an integer `code` from 400 to 599 | Engine failure with status `code` | Engine error message                                |
| Upstream HTTP `200` carrying any other error object                                 | Engine failure with status `500`  | Engine error message                                |

### Decode timeouts

| Timeout                           | Window                                                                                          |
| --------------------------------- | ----------------------------------------------------------------------------------------------- |
| `engine.first_token_timeout_s`    | From decode request start, across connection and response headers, to the first generated token |
| `engine.decode_read_timeout_s`    | Silence between any two transport chunks after the first token                                  |
| `engine.decode_read_timeout_s: 0` | Original request deadline as the stream bound                                                   |

| Expiry                         | HTTP  | Journal `error` contains            |
| ------------------------------ | :---: | ----------------------------------- |
| `engine.first_token_timeout_s` | `504` | `no first token within`             |
| `engine.decode_read_timeout_s` | `504` | `engine went silent between tokens` |

### Retries

`serving.max_attempts` sets the prefill/decode attempts per admitted request, from `1` (default) to `3`.

A fresh attempt starts for a transient fault before visible output when:

- the original request deadline permits it
- retry budget remains

Each retry receives:

- fresh backend request IDs
- a new KV handoff

Failed-engine placement with `recovery.failure_quarantine_s > 0`:

| Condition                                                                                               | Placement after the failure                          |
| ------------------------------------------------------------------------------------------------------- | ---------------------------------------------------- |
| Every role the failed engine places stays placeable through other live engines                          | Held out for `recovery.failure_quarantine_s` seconds |
| Another engine's ejection or drain leaves zero other live engines for a role the held-out engine places | Returned to placement                                |
| Any other case                                                                                          | Kept in placement                                    |
