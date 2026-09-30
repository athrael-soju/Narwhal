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

[Preflight](../deploy/06-Profile-and-Preflight.md#run-preflight) validates the pinned engine contract and probes role-permitted KV transfers.

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

Narwhal records two durations from the original request's arrival at the router:

- `ttft_s`: elapsed time to producer HTTP completion.
- `first_byte_s`: elapsed time to the first generated decode output observed by the router.

Their difference, `first_byte_s - ttft_s`, gives the interval between those events. See the [measurement contract](../measure/01-Profile.md).

### Python API

Python callers import:

```python
from narwhal.engines.client import EngineClient
from narwhal.engines.connector import PrefillResult
```

Pass the result of `EngineClient.prefill()` directly to `EngineClient.decode()`. For inspection, `result.parameters()` returns a detached dictionary.

Internal Python APIs may change between releases.

---

## Engine failure handling

Narwhal maps timeout-shaped engine faults to HTTP `504` and other engine faults to HTTP `502`.

Narwhal counts a nonempty `prompt` array of nonnegative integer token IDs locally. For text and chat input, when `engine.tokenize` is enabled and the configured dialect has an exact-count endpoint, a tokenisation timeout returns HTTP `504` before placement. Other tokenisation failures return an engine error. Each exact count goes to the live engine that holds the fewest requests. A failed count excludes its engine from later counts until another count succeeds. Character-ratio sizing applies when token counting is disabled or the dialect omits the exact-count endpoint.

Prefill finishes before client streaming begins, so prefill failures can be returned as ordinary HTTP errors.

### Breaker readmission

When `engine_contract` is configured, breaker readmission runs lifecycle validation. Development fleets that rely on health checks can readmit an ejected engine after a successful check.

The breaker classifies each failed decode leg:

| Failure | Engine | Class | Verification at `recovery.eject_after` consecutive failures |
| --- | --- | --- | --- |
| First-token timeout | Emitted other output during the wait | `overload` | Health probe |
| First-token timeout | Silent during the wait | `stream` | Inference probe |
| Mid-stream silence | Any | `stream` | Inference probe |

An inference-probe suspect leaves placement until the probe succeeds:

| Suspect | Placement during verification |
| --- | --- |
| Another live engine serves its role or accepts role changes | Held out |
| Its removal leaves its role unserved | Kept |

After a crossed-decode failure, the probe uses a new handoff produced by the original producer.

Each probe leg has a budget of the larger of `engine.first_token_timeout_s` and `engine.health_timeout_s`.

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

With `recovery.failure_quarantine_s` above zero, a failed engine's placement follows its role coverage:

| Engine | Placement after the failure |
| --- | --- |
| Another live engine covers its role | Held out for `recovery.failure_quarantine_s` seconds |
| Its removal leaves its role unserved | Kept |

A decode failure after HTTP `200` has already been committed emits a terminal SSE event:

```text
data: {"error": ...}
```
