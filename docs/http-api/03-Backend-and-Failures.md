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

Narwhal counts a nonempty `prompt` array of nonnegative integer token IDs locally. For text and chat input, when `engine.tokenize` is enabled and the configured dialect has an exact-count endpoint, a tokenisation timeout returns HTTP `504` before placement. Other tokenisation failures return an engine error. Character-ratio sizing applies when token counting is disabled or the dialect omits the exact-count endpoint.

Prefill finishes before client streaming begins, so prefill failures can be returned as ordinary HTTP errors.

### Breaker readmission

When `engine_contract` is configured, breaker readmission runs lifecycle validation. Development fleets that rely on health checks can readmit an ejected engine after a successful check.

After `recovery.eject_after` consecutive stream failures, Narwhal removes the engine from placement until an inference probe succeeds.

The failure streak includes:

- first-token timeout
- mid-stream silence

After a crossed-decode failure, the probe uses a new handoff produced by the original producer.

Each probe leg uses `engine.first_token_timeout_s`.

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

For ordinary requests, an error object carried inside an upstream HTTP `200`
stream propagates with the error object's own status.

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

If retries or opted-in continuation cannot recover a decode failure,
Narwhal emits a terminal SSE event after HTTP `200`:

```text
data: {"error": ...}
```

### Continuation failures

The unreleased [continuation feature](01-Requests.md#continuation-opt-in)
can recover eligible failures after output commits. Recovery uses the original
deadline and separate attempt limits and credits.

The router verifies a selected engine's qualification before each prefill or
decode dispatch, then rechecks the decode process after opening its stream.
A missing or stale qualification returns HTTP `503` before response headers,
or a terminal SSE error after headers. This failure does not count against
the engine's circuit breaker.

Continuation reads complete SSE events and verifies the echoed prompt IDs,
generated IDs, finish metadata and final terminator. EOF before a complete
`[DONE]` can trigger recovery from the preceding committed prefix. Malformed
events end the request with a fixed error message. Exceeding
the retained-history or original output limit emits an explicit stream error
and closes the response. Pending output stays uncommitted. A valid completion
at exactly `max_tokens` finishes normally with `finish_reason: "length"`.

Recovery admission errors use `type: "continuation_error"` and one of these
fixed codes and messages:

| Code | Message |
| --- | --- |
| `continuation_attempt_limit` | `Continuation recovery attempt limit reached` |
| `continuation_shared_budget` | `Continuation recovery credits are exhausted` |
| `continuation_no_survivor` | `No qualified continuation survivor is available` |
| `continuation_qualification` | `Qualified continuation capacity is unavailable` |
| `continuation_original_deadline` | `Original request deadline expired during continuation` |
| `continuation_output_limit` | `No output tokens remain for continuation` |
| `continuation_fenced` | `Router control does not permit continuation` |
| `continuation_prediction` | `Continuation prefill estimate exceeds the remaining deadline` |

Other failures retain their existing error type and code, including `expired`
when the request deadline interrupts an active operation. A committed
terminal group completes the request; later upstream cleanup cannot emit
another completion or error.

A failed or cancelled client write terminates the original request because
the server may have accepted part of that write. Continuation buffers remain
reserved until the router finishes closing the response, including while a
client write is blocked. Prompt text, generated text and token arrays are excluded from
continuation diagnostics and journal error details.
