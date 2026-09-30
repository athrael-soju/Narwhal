# Running requests on engines

## Disaggregated backend execution

Each request runs in two legs: prefill on a producer engine, then decode.

### Prefill

The producer receives a non-streaming completion request capped at one token. Narwhal keeps the KV handoff descriptor from the response and discards the generated token. The resulting `PrefillResult` ties that backend-owned descriptor to the producer URL, the endpoint, and the backend request ID.

### Decode

Remote decode receives the original prompt or messages, the requested output limit, the sampling settings, and the validated handoff descriptor. Every token the client sees comes from decode.

When decode runs on the same worker that did prefill, Narwhal strips all transfer parameters, including any the client sent. The engine then reuses its prefix cache or recomputes the prompt.

### Descriptor validation

Narwhal checks the descriptor before the decode HTTP request goes out. It rejects the descriptor if the engine identity is missing, the block IDs are malformed, or the connector or endpoint does not match. Opaque runtime fields that Narwhal does not interpret are passed through.

[Preflight](../deploy/06-Profile-and-Preflight.md#run-preflight) checks the pinned engine contract ahead of time and probes each KV transfer the roles allow.

### Ownership and timing

The original client request owns the handoff from start to finish. It enforces handoff age, holds the phase reservations, and handles retries and cleanup. The handoff-age clock starts when the producer HTTP leg starts. A retry gets fresh backend request IDs and a new producer owner.

Narwhal records two durations, measured from router arrival:

- `ttft_s` runs until the producer HTTP leg completes.
- `first_byte_s` runs until the router sees the first generated decode output.

See the [measurement contract](../measure/01-Profile.md) for how these are used.

### Python API

```python
from narwhal.engines.client import EngineClient
from narwhal.engines.connector import PrefillResult
```

Pass the value returned by `EngineClient.prefill()` to `EngineClient.decode()`. `result.parameters()` returns a detached dictionary. These are internal APIs and can change between releases.

## Engine failure handling

Engine faults that look like timeouts become HTTP `504`. Anything else from the engine becomes `502`. The client receives a generic message such as `Upstream request failed`; the full engine detail goes to the `error` field of the [request journal](../telemetry/01-Journal.md#terminal-request-records).

If `prompt` is a nonempty array of nonnegative integer token IDs, Narwhal counts it locally. For text and chat input, Narwhal asks the engine for an exact count when `engine.tokenize` is on and the dialect has an exact-count endpoint. A timeout on that call returns `504` before the request is placed, and any other tokenization failure returns an engine error. With token counting off, or on a dialect without an exact-count endpoint, Narwhal estimates size from a character ratio.

Prefill finishes before anything streams to the client, so a prefill failure returns as an ordinary HTTP error.

### Breaker readmission

After `recovery.eject_after` consecutive stream failures on an engine, first-token timeouts and mid-stream silence included, Narwhal removes it from placement until an inference probe succeeds. Each leg of the probe gets `engine.first_token_timeout_s`. If the engine failed during a crossed decode, the probe uses a fresh handoff from the original producer. An inconclusive probe waits for the next regular readmission attempt.

When `engine_contract` is configured, breaker readmission also runs lifecycle validation. Development fleets that rely on health checks can readmit an ejected engine once a check succeeds. The whole-wave restart policy applies in both cases.

### Successful stream termination

A good engine stream sends its generated output and ends with `data: [DONE]`. A stream that closes without `[DONE]` counts as an engine failure. If `[DONE]` arrives before any token, the attempt fails as a `502` engine error, and the journal's `error` field includes:

```text
stream ended with [DONE] before any token arrived
```

If the upstream responds with HTTP `200` and puts an error object in the stream, Narwhal treats it as an engine failure with the error object's status, or `500` if it has none. That status decides whether the attempt is retried and how the breaker counts it. A non-streaming client gets `504` for a `408` or `504` status and `502` for any other status.

### Decode timeouts

Two timeouts cover decode. `engine.first_token_timeout_s` starts before Narwhal opens the decode HTTP stream and runs until the first generated token, so connecting and waiting for response headers share one budget. After the first token, `engine.decode_read_timeout_s` limits how long the stream can go quiet between transport chunks. Metadata chunks count as activity and reset it. A value of 0 disables it, and the original request deadline alone bounds the stream.

When it fires, the attempt fails with status `504`, and the journal's `error` field includes:

```text
engine went silent between tokens
```

### Retries

By default each admitted request gets one prefill/decode attempt. With `serving.max_attempts` above 1, if a transient fault happens before any output reaches the client, Narwhal can start a fresh attempt, provided the original request deadline allows it and there is retry budget left.

When `recovery.failure_quarantine_s` is above zero, the failed engine is briefly kept out of placement while the breaker catches up.

After HTTP `200` has gone out, the status is fixed. If decode fails after that point, Narwhal ends the stream with an error event:

```text
data: {"error": ...}
```
