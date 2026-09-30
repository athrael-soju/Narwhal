# Admission and responses

## Admission and refusal semantics

The `error` object in the response body carries the `type` listed below.

The time to first token (TTFT) budget is `slo.ttft_s * (1 + serving.admission_margin)`.

| Condition                                                                         |  HTTP | Error `type`                                     | `Retry-After`                                |
| --------------------------------------------------------------------------------- | ----: | ------------------------------------------------ | -------------------------------------------- |
| Malformed JSON or a wrong type in a field the router reads                        | `400` | `invalid_request_error`; `param` names the field |                                              |
| Requested model differs from the configured model                                 | `404` | `invalid_request_error`, code `model_not_found`  |                                              |
| `n > 1` or `best_of > 1`                                                          | `400` | `invalid_request_error`                          |                                              |
| Unsupported non-streaming audio, modality, or tool request                        | `400` | `invalid_request_error`; `param` names the option |                                             |
| Request exceeds `serving.max_request_bytes`                                       | `413` | `request_too_large`                              |                                              |
| HTTP retention limit is full                                                      | `429` | `server_overloaded_error`                        | `1`                                          |
| Admission queue is full                                                           | `429` | `server_overloaded_error`                        | `1`                                          |
| Admission wait expires before response headers                                    | `504` | `queue_expired`                                  |                                              |
| Original request deadline expires before response headers                         | `504` | `request_expired` or `expired`                   |                                              |
| Projected TTFT exceeds the budget; the prompt alone fits                          | `429` | `server_overloaded_error`                        | Budget overrun in seconds, rounded up, minimum `1` |
| Prompt alone exceeds the TTFT budget                                              | `429` | `server_overloaded_error`                        |                                              |
| Zero engines are eligible for placement                                           | `503` | `backend_unavailable`                            | `1`                                          |
| Router is standby, fenced, in whole-wave maintenance, awaiting engine identity validation, or in degraded engine monitoring | `503` | `standby` | `1` |

Shorten the prompt or raise `slo.ttft_s` to clear a 429 for an oversized prompt.

`/ready` reports the reason whenever the router refuses a request with `503`.

[`serving.admission`](../configuration/02-Serving-and-Role-Control.md#41-global-admission) modes:

| Mode                   | Enforced                                                                                  |
| ---------------------- | ----------------------------------------------------------------------------------------- |
| `predictive` (default) | Both predictive TTFT checks, plus the HTTP retention, queue, and phase-concurrency limits |
| `open`                 | HTTP retention, queue, and phase-concurrency limits                                       |

### Admission counters

| Counter            | Increments on                              |
| ------------------ | ------------------------------------------ |
| `offered`          | Each request to a completion route         |
| `unsized_offered`  | Termination before workload sizing         |
| `rejected`         | Global capacity rejection                  |
| `refused`          | Global predictive refusal                  |
| `invalid_requests` | Malformed request body                     |

---

## Streaming and response assembly

### Streaming responses

Narwhal forwards streaming deltas in the engine's response shape, with the [token-ID rules](#token-identity-and-output-accounting) applied.

### Non-streaming assembly

Narwhal assembles the engine stream into one response:

| Engine output            | Assembly behaviour                                                                                                        |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------- |
| Chat `content`           | String deltas concatenate under `content`                                                                                 |
| Chat `reasoning`         | String deltas concatenate under `reasoning`                                                                               |
| Chat `reasoning_content` | String deltas concatenate under `reasoning_content`                                                                       |
| Chat `refusal`           | String deltas concatenate under `refusal`                                                                                 |
| `tool_calls`             | Calls are grouped by stream index and returned in index order. ID, function name, and arguments concatenate separately |
| Legacy `function_call`   | Function name and argument fragments concatenate into one message field                                                   |
| Chat logprobs            | Content and refusal arrays concatenate in stream order                                                                    |
| Text-completion logprobs | Token, logprob, and offset arrays concatenate in stream order                                                             |

Tool arguments return as engine-generated strings.

Assembly returns HTTP `502` when:

- an unsupported choice or chat-delta field carries a value, such as audio, annotations, or custom tool output
- a supported field has a malformed value
- a tool call is missing its ID or function name

### Metadata and usage

Assembly keeps the response metadata and derives the fields below.

| Field                              | Source                                                                                  |
| ---------------------------------- | --------------------------------------------------------------------------------------- |
| `finish_reason`, `stop_reason`     | Last choice that reports each; `stop_reason` is optional                                |
| `usage`                            | Non-null engine `usage`, including a later usage-only frame                             |
| `usage`, when the engine omits it  | Computed from input length and measured output token count                              |

---

## Token identity and output accounting

Decode requests to supporting engines include:

```json
{
  "return_token_ids": true,
  "stream_interval": 1
}
```

Every event with text, reasoning, tool calls, or refusals must carry a list of nonnegative integer token IDs. A missing or malformed list, including a Boolean ID, fails the decode attempt or profiling measurement.

Validated IDs feed output length, time per output token (TPOT) timing, decode correction, drift scoring, and output-length learning.

| Engine                   | Router reports                   |
| ------------------------ | -------------------------------- |
| Supplies token IDs       | `token_accounting: token_ids`    |
| Omits token IDs          | `token_accounting: unavailable`  |

Clients that set `return_token_ids` receive the IDs in streaming and assembled responses.
