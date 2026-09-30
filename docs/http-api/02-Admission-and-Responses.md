# How requests are admitted and answered

## Admission and refusal semantics

Conditions under which a completion request is refused or expires before response headers are sent. `TTFT` is time to first token, and the budget is the configured TTFT target.

| Condition                                                                         |  HTTP | Result                                                       |
| --------------------------------------------------------------------------------- | ----: | ------------------------------------------------------------ |
| Invalid JSON, wrong body shape, or wrong type on a field the router reads         | `400` | `invalid_request_error`; `param` names the field             |
| `model` differs from the configured model                                         | `404` | `model_not_found`                                            |
| `n > 1` or `best_of > 1`                                                          | `400` | Invalid sampling width                                       |
| Non-streaming request asks for audio, a non-text modality, or a non-function tool | `400` | `invalid_request_error`; `param` names the option            |
| Body larger than `serving.max_request_bytes`                                      | `413` | `request_too_large`                                          |
| HTTP retention limit or admission queue is full                                   | `429` | `server_overloaded_error`, `Retry-After: 1`                  |
| Admission wait or the original request deadline expires                           | `504` | Terminal expiry; error type `queue_expired`, `request_expired`, or `expired`, depending on where it expired |
| Predicted total TTFT exceeds the budget and the prompt alone fits                | `429` | `server_overloaded_error`; `Retry-After` is the projected overrun rounded up to whole seconds, minimum 1 |
| The prompt alone exceeds the TTFT budget                                          | `429` | `server_overloaded_error`, no `Retry-After`; shorten the prompt or raise the TTFT target |
| No live prefill engine, and the chosen engine has resident decode work (prefill cannot be priced) | `429` | `server_overloaded_error`, `Retry-After: 1` |
| No engine is eligible for placement                                               | `503` | `backend_unavailable` (`no_schedulable_engines` if placement fails before the first prefill), `Retry-After: 1` |
| Router is standby, fenced, in whole-wave maintenance, monitoring-degraded, or still validating engine identities | `503` | `standby`, `Retry-After: 1`; the message and `/ready` give the reason |

The TTFT refusals above apply only when `serving.admission` is `predictive` (the default). With `open`, placed requests go straight to prefill, subject to the HTTP retention, queue, and phase-concurrency limits.

### Admission counters

Every request to a completion route counts once in `offered`. If it ends before Narwhal reads and sizes its body, for example because it was turned away first, it also counts in `unsized_offered`.

Turned-away requests are counted separately:

- `rejected`: capacity rejections, including not-ready `503` responses.
- `refused`: predictive TTFT refusals.
- `invalid_requests`: `400`, `404`, and `413` responses.

## Streaming and response assembly

### Streaming responses

Streaming responses pass through the engine's delta fields unchanged, except that Narwhal withholds metadata frames until the first generated token, so a retried attempt does not repeat them, and may expose token IDs. See [Token identity and output accounting](#token-identity-and-output-accounting).

### Non-streaming assembly

For non-streaming requests, Narwhal streams from the engine, reads the whole stream, and assembles a single response:

| Engine output                                               | How it's assembled                                                                                                         |
| ----------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| Chat `content`, `reasoning`, `reasoning_content`, `refusal` | String deltas for each field are concatenated into that field                                                              |
| `tool_calls`                                                | Grouped by stream index and returned in index order; the ID, function name, and arguments are each concatenated separately |
| Legacy `function_call`                                      | Name and argument fragments are concatenated into a single message field                                                   |
| Chat logprobs                                               | Content and refusal arrays are concatenated in stream order                                                                |
| Text-completion logprobs                                    | Token, logprob, and offset arrays are concatenated in stream order                                                         |

Each assembled tool call requires an ID and a function name. Arguments are returned as the raw string the engine generated; the client parses them.

Assembly returns HTTP `502` if a choice or chat delta contains an unsupported field (for example audio, annotations, or custom tool output) or a malformed supported field.

### Metadata and usage

The assembled response keeps the engine's response metadata, its `usage` object when it isn't null, `finish_reason`, and `stop_reason` if the engine sent one. When the engine sends no usage and Narwhal counted output token IDs, Narwhal fills usage in from the input length and that count.

Some engines send a usage-only frame after the frame that carries `finish_reason`. That trailing frame doesn't overwrite the finish or stop reason.

## Token identity and output accounting

When the engine supports it, Narwhal asks for token IDs on the decode stream:

```json
{
  "return_token_ids": true,
  "stream_interval": 1
}
```

Each event that carries generated text, reasoning, tool calls, or a refusal must include the ID list, and Narwhal counts its IDs (nonnegative integers). A missing or malformed list, including one that contains a Boolean, fails the decode attempt or profiling measurement.

Narwhal uses the IDs to measure output length and per-token timing (time per output token, TPOT), which feed TPOT scoring, decode correction, drift scoring, and output-length learning.

The router reports `token_accounting: token_ids` for engines that return IDs and `token_accounting: unavailable` for engines that don't. If a client sets `return_token_ids` itself, the IDs are returned in both streaming and assembled responses.
