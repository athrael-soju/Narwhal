# Backend execution and failures

## Disaggregated backend execution

### Prefill

Narwhal sends the producer a non-streaming, one-token completion request and discards the generated token after capturing the KV handoff descriptor.

`PrefillResult` associates the backend-owned KV descriptor with:

- producer URL
- endpoint
- backend request ID

### Decode

Remote decode receives:

- the original prompt or messages
- the requested output limit
- sampling settings
- the validated handoff descriptor

Decode generates every client-visible output token.

For same-worker decode, Narwhal removes transfer parameters, including client-provided values, and relies on engine prefix caching or prompt recomputation.

### Descriptor validation

Narwhal validates the descriptor before making the decode HTTP request.

It rejects:

- missing engine identity
- malformed block IDs
- connector mismatch
- endpoint mismatch

Opaque runtime fields are retained.

Preflight and occupied-role canaries verify KV transfer against the pinned engine contract.

### Ownership and timing

The original client request owns:

- handoff-age enforcement
- phase reservations
- retries
- cleanup

Handoff age begins when the producer HTTP leg starts.

Every retry receives:

- fresh backend request IDs
- new producer ownership

Narwhal measures these intervals separately:

1. producer HTTP completion
2. first visible decode output
3. handoff time between the two

### Python API

Python callers import:

```python
from narwhal.engines.client import EngineClient
from narwhal.engines.connector import PrefillResult
```

Pass the result of:

```python
EngineClient.prefill()
```

directly to:

```python
EngineClient.decode()
```

For inspection:

```python
result.parameters()
```

returns a detached dictionary.

Internal Python APIs may change between releases.

---

## Engine failure handling

Narwhal maps timeout-shaped engine faults to HTTP `504` and other engine faults to HTTP `502`.

Prefill finishes before client streaming begins, so prefill failures can be returned as ordinary HTTP errors.

### Breaker readmission

When `engine_contract` is configured, breaker readmission runs lifecycle validation. Development fleets that rely on health checks can readmit an ejected engine after a successful check.

After:

```text
recovery.eject_after
```

consecutive stream failures, Narwhal removes the engine from placement until an inference probe succeeds.

The failure streak includes:

- first-token timeout
- mid-stream silence

After a crossed-decode failure, the probe uses a new handoff produced by the original producer.

Each probe leg uses:

```text
engine.first_token_timeout_s
```

Inconclusive probes return to the normal readmission cadence.

Whole-wave restart policy continues to apply.

### Successful stream termination

A valid engine stream contains:

1. generated output
2. `data: [DONE]`

Closing the stream before `[DONE]` is an engine failure.

Receiving `[DONE]` before the first generated token returns HTTP `502` with:

```text
stream ended with [DONE] before any token arrived
```

An error object carried inside an upstream HTTP `200` stream propagates with the error object's own status.

### Decode timeouts

`engine.first_token_timeout_s` limits the time between opening the decode stream and receiving the first generated token.

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

When:

```text
recovery.failure_quarantine_s > 0
```

the failed engine is temporarily excluded from subsequent placement while breaker state catches up.

A decode failure after HTTP `200` has already been committed emits a terminal SSE event:

```text
data: {"error": ...}
```
