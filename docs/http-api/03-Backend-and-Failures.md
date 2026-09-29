# Backend execution and failures

## Disaggregated backend execution

### Prefill

Narwhal sends the producer a non-streaming, one-token completion request and discards the generated token after capturing the KV handoff descriptor.

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

Narwhal validates the KV handoff descriptor before the decode HTTP request and rejects:

- missing engine identity
- malformed block IDs
- a connector mismatch
- an endpoint mismatch

Narwhal retains opaque runtime fields.

[Preflight](../deploy/06-Profile-and-Preflight.md#run-preflight) validates the pinned engine contract and probes role-permitted KV transfers.

### Ownership and timing

The original client request owns:

- handoff-age enforcement
- phase reservations
- retries
- cleanup

Handoff age begins when the producer HTTP leg starts.

Each retry gets fresh backend request IDs and a new KV handoff.

Durations from the request's arrival at the router:

- `ttft_s`: time to producer HTTP completion.
- `first_byte_s`: time to the first generated decode output observed by the router.

`first_byte_s - ttft_s` measures the time from producer completion to the first decode output. See [Measurement contract and profiling](../measure/01-Profile.md).

### Python API

Python callers import:

```python
from narwhal.engines.client import EngineClient
from narwhal.engines.connector import PrefillResult
```

Pass the `EngineClient.prefill()` result to `EngineClient.decode()`. `result.parameters()` returns a detached dictionary.

---

## Engine failure handling

Timeout-shaped engine faults map to HTTP `504`. Other engine faults map to HTTP `502`.

| Failure                                                     | Client response                                     |
| ----------------------------------------------------------- | --------------------------------------------------- |
| Prefill, streaming or non-streaming                         | HTTP error status                                   |
| Decode, non-streaming                                       | HTTP error status                                   |
| Decode, streaming, outside the [retry conditions](#retries) | A terminal server-sent event (SSE) after HTTP `200` |

A streaming response commits HTTP `200` before decode starts.

Terminal SSE event:

```text
data: {"error": ...}
```

The decode status codes below apply to non-streaming requests.

The client error body carries a generic message, such as `Upstream request failed` and the engine's failure detail goes to the `error` field of the [terminal request record](../telemetry/01-Journal.md#terminal-request-records).

### Input sizing

| Input                                                                              | Sizing before placement    |
| ---------------------------------------------------------------------------------- | -------------------------- |
| Nonempty `prompt` array of nonnegative integer token IDs                           | Local ID count             |
| Text or chat, with `engine.tokenize` on and an exact-count endpoint in the dialect | Exact count from an engine |
| Other input                                                                        | Character ratio            |

A tokenization timeout returns HTTP `504` before placement. Other tokenization failures follow the [engine-fault mapping](#engine-failure-handling).

### Breaker readmission

After `recovery.eject_after` consecutive stream failures, Narwhal ejects the engine until an inference probe succeeds. Stream failures:

- first-token timeout
- mid-stream silence
- stream closed before `[DONE]`
- `[DONE]` before any token

After a crossed-decode failure, the probe uses a new handoff produced by the original producer.

Each probe leg uses `engine.first_token_timeout_s`.

Inconclusive probes return to the normal readmission cadence.

Whole-wave restart policy continues to apply.

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

An error object carried inside an upstream HTTP `200` stream propagates with the error object's own status.

### Decode timeouts

`engine.first_token_timeout_s` starts before opening the decode HTTP stream and ends at the first generated token. Connection and response-header delays consume the same budget.

After the first token, `engine.decode_read_timeout_s` bounds silence between transport chunks; metadata chunks reset the timer.

Expiry returns HTTP `504` with detail beginning:

```text
engine went silent between tokens
```

A zero `engine.decode_read_timeout_s` uses the original request deadline as the stream bound.

### Retries

Each admitted request receives one prefill/decode attempt by default.

Before visible output, Narwhal may start a fresh attempt for a transient fault when both conditions hold:

- the original request deadline still permits it
- retry budget remains

When `recovery.failure_quarantine_s > 0`, the failed engine is temporarily excluded from subsequent placement while breaker state catches up.

A decode failure after HTTP `200` has already been committed emits a terminal SSE event:

```text
data: {"error": ...}
```
