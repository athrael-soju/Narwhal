# How requests are admitted and answered

## Admission and refusal semantics

This table covers the ways a completion request can be turned away or cut short before response headers are sent.

| Condition                                                                         |  HTTP | Result                                                       |
| --------------------------------------------------------------------------------- | ----: | ------------------------------------------------------------ |
| Invalid JSON, wrong body shape, or wrong type on a field the router reads         | `400` | `invalid_request_error`; `param` names the field             |
| `model` differs from the configured model                                         | `404` | `model_not_found`                                            |
| `n > 1` or `best_of > 1`                                                          | `400` | Invalid sampling width                                       |
| Non-streaming request asks for audio, a non-text modality, or a non-function tool | `400` | `invalid_request_error`; `param` names the option            |
| Body larger than `serving.max_request_bytes`                                      | `413` | `request_too_large`                                          |
| HTTP retention limit or admission queue is full                                   | `429` | `server_overloaded_error`, `Retry-After: 1`                  |
| Admission wait or the original request deadline expires                           | `504` | Terminal expiry; error type `queue_expired`, `request_expired`, or `expired`, depending on where it expired |
| Predicted total TTFT exceeds the budget, but the prompt alone would fit           | `429` | `server_overloaded_error`; `Retry-After` is the projected overrun rounded up to whole seconds, minimum 1 |
| The prompt alone exceeds the TTFT budget                                          | `429` | `server_overloaded_error`, no `Retry-After`; shorten the prompt or raise the TTFT target |
| No prefill engine is live and the chosen engine has resident decode work, so prefill can't be priced | `429` | `server_overloaded_error`, `Retry-After: 1` |
| No engine is eligible for placement                                               | `503` | `backend_unavailable` (`no_schedulable_engines` if placement fails before the first prefill), `Retry-After: 1` |
| Router is standby, fenced, in whole-wave maintenance, monitoring-degraded, or still validating engine identities | `503` | `standby`, `Retry-After: 1`; the message and `/ready` give the reason |

With `serving.admission: open`, placed requests go straight to prefill, subject to the HTTP retention, queue, and phase-concurrency limits. The TTFT refusals above apply only to the default, `predictive`.

### Admission counters

Every request to a completion route counts once in `offered`. If it ends before Narwhal reads and sizes its body, for example because it was turned away first, it also counts in `unsized_offered`.

Refusals have their own counters: capacity rejections, including not-ready `503` refusals, go to `rejected`; predictive refusals go to `refused`; and requests rejected as invalid (`400`, `404`, or `413`) go to `invalid_requests`.

## Streaming and response assembly

### Streaming responses

Streaming clients receive the engine's delta fields in the engine's own response shape. Narwhal holds back metadata frames until the first generated token, so a retried attempt doesn't repeat them. Otherwise it changes only token-ID exposure, described [below](#token-identity-and-output-accounting).

### Non-streaming assembly

Narwhal still streams from the engine when the client asked for a non-streaming response. It reads the whole stream and builds one final response from it:

| Engine output                                               | How it's assembled                                                                                                         |
| ----------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| Chat `content`, `reasoning`, `reasoning_content`, `refusal` | String deltas for each field are concatenated into that field                                                              |
| `tool_calls`                                                | Grouped by stream index and returned in index order; the ID, function name, and arguments are each concatenated separately |
| Legacy `function_call`                                      | Name and argument fragments are concatenated into a single message field                                                   |
| Chat logprobs                                               | Content and refusal arrays are concatenated in stream order                                                                |
| Text-completion logprobs                                    | Token, logprob, and offset arrays are concatenated in stream order                                                         |

Each assembled tool call has to have an ID and a function name. Arguments come back as the string the engine generated; parsing them is the client's job.

The assembler returns HTTP `502` when a choice or chat delta carries a value in a field it doesn't support, such as audio, annotations, or custom tool output, or when a supported field is malformed.

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

Every event that carries generated text, reasoning, tool calls, or a refusal has to include the ID list, and Narwhal counts the IDs that are nonnegative integers. If the list is missing, contains a Boolean, or is otherwise malformed, the decode attempt or profiling measurement fails.

These IDs are how Narwhal knows output length and per-token timing for TPOT scoring. The same output also drives decode correction, drift scoring, and output-length learning.

The router reports `token_accounting: token_ids` for engines that return IDs and `token_accounting: unavailable` for dialects that don't. Clients that set `return_token_ids` themselves get the IDs back in both streaming and assembled responses.
