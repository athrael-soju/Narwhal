# Admission and responses

## Admission and refusal semantics

The time to first token (TTFT) budget is `slo.ttft_s * (1 + serving.admission_margin)`.

| Condition                                                                         |  HTTP | Error `type`                                     | `Retry-After`                                |
| --------------------------------------------------------------------------------- | ----: | ------------------------------------------------ | -------------------------------------------- |
| Malformed JSON or a wrong type in a field the router reads                        | `400` | `invalid_request_error` with the field in `param` |                                              |
| Requested model differs from the configured model                                 | `404` | `invalid_request_error`, code `model_not_found`  |                                              |
| `n > 1` or `best_of > 1`                                                          | `400` | `invalid_request_error`                          |                                              |
| Unsupported non-streaming audio, modality, or tool request                        | `400` | `invalid_request_error` with the option in `param` |                                             |
| Request exceeds `serving.max_request_bytes`                                       | `413` | `request_too_large`                              |                                              |
| HTTP retention limit is full                                                      | `429` | `server_overloaded_error`                        | `1`                                          |
| Admission queue is full                                                           | `429` | `server_overloaded_error`                        | `1`                                          |
| Admission wait expires before response headers                                    | `504` | `queue_expired`                                  |                                              |
| Original request deadline expires before response headers                         | `504` | `request_expired` or `expired`                   |                                              |
| Projected TTFT exceeds the budget for a prompt that fits alone                          | `429` | `server_overloaded_error`                        | Budget overrun in seconds, rounded up, minimum `1` |
| Prompt alone exceeds the TTFT budget                                              | `429` | `server_overloaded_error`                        |                                              |
| Zero engines are eligible for placement                                           | `503` | `backend_unavailable`                            | `1`                                          |
| Router is standby, fenced, in whole-wave maintenance, awaiting engine identity validation, or in degraded engine monitoring | `503` | `standby` | `1` |

Shorten the prompt or raise `slo.ttft_s` to clear a 429 for an oversized prompt.

`/ready` reports the reason for each `503` refusal.

[`serving.admission`](../configuration/02-Serving-and-Role-Control.md#41-global-admission) modes:

| Mode                   | Enforced                                                                                  |
| ---------------------- | ----------------------------------------------------------------------------------------- |
| `predictive` (default) | Predictive TTFT checks and the HTTP retention, queue, and phase-concurrency limits |
| `open`                 | HTTP retention, queue, and phase-concurrency limits                                       |

### Admission counters

| Counter            | Increments on                              |
| ------------------ | ------------------------------------------ |
| `offered`          | Each request to a completion route         |
| `unsized_offered`  | Termination before workload sizing         |
| `rejected`         | Global capacity rejection                  |
| `refused`          | Global predictive refusal                  |
| `invalid_requests` | Malformed request body                     |

## Streaming and response assembly

### Streaming responses

Streaming deltas use the engine's response shape under the [token-ID rules](#token-identity-and-output-accounting).

### Non-streaming assembly

Non-streaming responses assemble the engine stream as follows:

| Engine output            | Assembly behaviour                                                                                                        |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------- |
| Chat `content`           | String deltas concatenate under `content`                                                                                 |
| Chat `reasoning`         | String deltas concatenate under `reasoning`                                                                               |
| Chat `reasoning_content` | String deltas concatenate under `reasoning_content`                                                                       |
| Chat `refusal`           | String deltas concatenate under `refusal`                                                                                 |
| `tool_calls`             | One call per stream index, returned in index order                                                                        |
| Tool call fields         | ID, function name, and arguments concatenate separately per call |
| Tool call arguments      | Returned as engine-generated strings |
| Legacy `function_call`   | Function name and argument fragments concatenate into one message field                                                   |
| Chat logprobs            | Content and refusal arrays concatenate in stream order                                                                    |
| Text-completion logprobs | Token, logprob, and offset arrays concatenate in stream order                                                             |

Assembly returns HTTP `502` when:

- an unsupported choice or chat-delta field carries a value, such as audio, annotations, or custom tool output
- a supported field has a malformed value
- a tool call is missing its ID or function name

### Metadata and usage

Assembled responses carry the engine's response metadata and these derived fields:

| Field                              | Source                                                                                  |
| ---------------------------------- | --------------------------------------------------------------------------------------- |
| `finish_reason`, `stop_reason`     | Last choice that reports each                                                           |
| `usage`                            | Non-null engine `usage`, including a later usage-only frame                             |
| `usage`, when the engine omits it  | Computed from input length and measured output token count                              |

## Token identity and output accounting

Decode requests to supporting engines include:

```json
{
  "return_token_ids": true,
  "stream_interval": 1
}
```

Every event with text, reasoning, tool calls, or refusals carries a token-ID list:

| Token-ID list                                | Result                                          |
| -------------------------------------------- | ----------------------------------------------- |
| Nonnegative integers                         | Accepted                                        |
| Missing or malformed, including a Boolean ID | Decode attempt or profiling measurement fails   |

`token_accounting` by engine:

| Engine                   | Router reports                   |
| ------------------------ | -------------------------------- |
| Supplies token IDs       | `token_accounting: token_ids`    |
| Omits token IDs          | `token_accounting: unavailable`  |

Clients that set `return_token_ids` receive the IDs in streaming and assembled responses.
