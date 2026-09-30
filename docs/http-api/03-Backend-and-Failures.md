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

- strips transfer parameters, including client-provided values
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

| Duration                | Interval                                                                   |
| ----------------------- | -------------------------------------------------------------------------- |
| `ttft_s`                | Request arrival at the router to producer HTTP completion                  |
| `first_byte_s`          | Request arrival at the router to the first generated decode output |
| `first_byte_s - ttft_s` | Producer completion to the first decode output                             |

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
| -------------- | ----- |
| Timeout-shaped | `504` |
| Other          | `502` |

| Failure                                                     | Client response                                     |
| ----------------------------------------------------------- | --------------------------------------------------- |
| Prefill, streaming or non-streaming                         | HTTP error status                                   |
| Decode, non-streaming                                       | HTTP error status                                   |
| Decode, streaming, outside the [retry conditions](#retries) | A terminal server-sent event (SSE) after HTTP `200` |

```text
data: {"error": ...}
```

| Destination                                                                                           | Content                                            |
| ----------------------------------------------------------------------------------------------------- | -------------------------------------------------- |
| Client error body                                                                                     | Generic message, such as `Upstream request failed` |
| `error` field of the [terminal request record](../telemetry/01-Journal.md#terminal-request-records) | Engine failure detail                              |

### Input sizing

| Input                                                                              | Sizing before placement    |
| ---------------------------------------------------------------------------------- | -------------------------- |
| Nonempty `prompt` array of nonnegative integer token IDs                           | Local ID count             |
| Text or chat, with `engine.tokenize` on and an exact-count endpoint in the dialect | Exact count from an engine |
| Other input                                                                        | Character ratio            |

Tokenization failures return the [engine-fault mapping](#engine-failure-handling) status before placement.

### Breaker readmission

| Event                                              | Breaker action      |
| -------------------------------------------------- | ------------------- |
| `recovery.eject_after` consecutive stream failures | Ejects the engine   |
| Successful inference probe                         | Readmits the engine |

Stream failures:

- first-token timeout
- mid-stream silence
- stream closed before `[DONE]`
- `[DONE]` before the first token

Inference probes apply `engine.first_token_timeout_s` to each leg.

### Successful stream termination

A valid engine stream ends with `data: [DONE]` after generated output.

| Stream                                       | Result                                                                  |
| -------------------------------------------- | ----------------------------------------------------------------------- |
| Closes before `[DONE]`                       | Engine failure                                                          |
| `[DONE]` before the first generated token    | HTTP `502`                                                              |
| Upstream HTTP `200` carrying an error object | Engine failure with status `code`, an integer in 400 to 599 |
| Upstream HTTP `200` carrying any other error object | Engine failure with status `500` |

Journal `error` for an early `[DONE]`:

```text
stream ended with [DONE] before any token arrived
```

### Decode timeouts

| Timeout                        | Window                                                                                                                                  |
| ------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------- |
| `engine.first_token_timeout_s` | Decode request start to the first generated token, connection and response-header delays included |
| `engine.decode_read_timeout_s` | Silence between transport chunks after the first token, metadata chunks included |
| `engine.decode_read_timeout_s: 0` | Original request deadline as the stream bound |

`engine.decode_read_timeout_s` expiry returns HTTP `504` with detail beginning:

```text
engine went silent between tokens
```

### Retries

Each admitted request receives one prefill/decode attempt by default.

A fresh attempt starts for a transient fault before visible output when:

- the original request deadline permits it
- retry budget remains

Each retry receives:

- fresh backend request IDs
- a new KV handoff

With `recovery.failure_quarantine_s > 0`, the failed engine is excluded from new placement for that many seconds.
