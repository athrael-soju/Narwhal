# Backend execution and failures

## Disaggregated backend execution

### Prefill

The prefill leg is a non-streaming, one-token completion request to the producer. Narwhal keeps the resulting KV handoff descriptor and discards the generated token.

`PrefillResult` holds the KV handoff descriptor, producer URL, endpoint, and backend request ID.

### Decode

Remote decode receives:

- the original prompt or messages
- the requested output limit
- sampling settings
- the validated handoff descriptor

Decode generates every client-visible output token.

Same-engine decode strips transfer parameters, including client-provided values, and uses engine prefix caching or prompt recomputation.

### Descriptor validation

[Preflight](../deploy/06-Profile-and-Preflight.md#run-preflight) validates the pinned engine contract and probes role-permitted KV transfers.

Narwhal validates the KV handoff descriptor before each decode HTTP request. It keeps opaque runtime fields and rejects a descriptor with:

- missing engine identity
- malformed block IDs
- a connector mismatch
- an endpoint mismatch

### Request scope and timing

These are scoped to the original client request:

- handoff-age enforcement
- phase reservations
- retries
- cleanup

Handoff age begins when the producer HTTP leg starts. Each retry gets fresh backend request IDs and a new KV handoff.

Durations in the [measurement contract](../measure/01-Profile.md):

| Duration                | Interval                                                                   |
| ----------------------- | -------------------------------------------------------------------------- |
| `ttft_s`                | Request arrival at the router to producer HTTP completion                  |
| `first_byte_s`          | Request arrival at the router to the first generated decode output it observes |
| `first_byte_s - ttft_s` | Producer completion to the first decode output                             |

### Python API

Python callers import:

```python
from narwhal.engines.client import EngineClient
from narwhal.engines.connector import PrefillResult
```

Pass the `EngineClient.prefill()` result to `EngineClient.decode()`. `result.parameters()` returns a detached dictionary.

---

## Engine failure handling

Engine faults map to HTTP status codes:

| Engine fault   | HTTP  |
| -------------- | ----- |
| Timeout-shaped | `504` |
| Other          | `502` |

A streaming response commits HTTP `200` before decode starts.

| Failure                                                     | Client response                                     |
| ----------------------------------------------------------- | --------------------------------------------------- |
| Prefill, streaming or non-streaming                         | HTTP error status                                   |
| Decode, non-streaming                                       | HTTP error status                                   |
| Decode, streaming, outside the [retry conditions](#retries) | A terminal server-sent event (SSE) after HTTP `200` |

Terminal SSE event:

```text
data: {"error": ...}
```

The decode status codes below apply to non-streaming requests.

Failure detail by destination:

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

A tokenization timeout returns HTTP `504` before placement. Other tokenization failures follow the [engine-fault mapping](#engine-failure-handling).

### Breaker readmission

Narwhal ejects an engine at `recovery.eject_after` consecutive stream failures and readmits it when an inference probe succeeds. Stream failures are:

- first-token timeout
- mid-stream silence
- stream closed before `[DONE]`
- `[DONE]` before any token

Inference probes:

- use a new handoff from the original producer for a crossed-decode failure
- apply `engine.first_token_timeout_s` to each leg
- return to the normal readmission cadence when inconclusive

### Successful stream termination

A valid engine stream ends with `data: [DONE]` after generated output.

| Stream                                       | Result                                                                  |
| -------------------------------------------- | ----------------------------------------------------------------------- |
| Closes before `[DONE]`                       | Engine failure                                                          |
| `[DONE]` before the first generated token    | HTTP `502`                                                              |
| Upstream HTTP `200` carrying an error object | Engine failure; status from integer `code` in 400 to 599, otherwise 500 |

Journal `error` for an early `[DONE]`:

```text
stream ended with [DONE] before any token arrived
```

### Decode timeouts

| Timeout                        | Window                                                                                                                                  |
| ------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------- |
| `engine.first_token_timeout_s` | Starts before the decode HTTP stream opens and ends at the first generated token; connection and response-header delays count against it |
| `engine.decode_read_timeout_s` | Silence between transport chunks after the first token; metadata chunks reset the timer; a zero value uses the original request deadline as the stream bound |

`engine.decode_read_timeout_s` expiry returns HTTP `504` with detail beginning:

```text
engine went silent between tokens
```

### Retries

Each admitted request receives one prefill/decode attempt by default.

Before visible output, Narwhal may start a fresh attempt for a transient fault when both conditions hold:

- the original request deadline permits it
- retry budget remains

With `recovery.failure_quarantine_s > 0`, Narwhal excludes the failed engine from new placement for that many seconds.

A decode failure after HTTP `200` is committed emits a terminal SSE event:

```text
data: {"error": ...}
```
