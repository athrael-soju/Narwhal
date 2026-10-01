---
description: TTFT-based admission, streaming and token accounting for Narwhal completion responses.
---

# Admission and responses

## Admission and refusal semantics

The time to first token (TTFT) budget is `slo.ttft_s * (1 + serving.admission_margin)`.

| Condition                                                                                                                    |  HTTP | Error `type`                                         | `Retry-After`                                      |
| ---------------------------------------------------------------------------------------------------------------------------- | :---: | ---------------------------------------------------- | -------------------------------------------------- |
| Malformed JSON or a wrong type in a field the router reads                                                                   | `400` | `invalid_request_error` with the field in `param`    |                                                    |
| `vllm_xargs` sets `kv_cache_report_mode`, `kv_transfer_params`, or `ec_transfer_params`                                      | `400` | `invalid_request_error` with `vllm_xargs` in `param` |                                                    |
| Requested model differs from the configured model                                                                            | `404` | `invalid_request_error`, code `model_not_found`      |                                                    |
| `n > 1` or `best_of > 1`                                                                                                     | `400` | `invalid_request_error`                              |                                                    |
| Unsupported non-streaming audio, modality, or tool request                                                                   | `400` | `invalid_request_error` with the option in `param`   |                                                    |
| Request exceeds `serving.max_request_bytes`                                                                                  | `413` | `request_too_large`                                  |                                                    |
| HTTP retention limit is full                                                                                                 | `429` | `server_overloaded_error`                            | `1`                                                |
| Router event-loop lag reaches a quarter of `slo.ttft_s`                                                                      | `429` | `server_overloaded_error`                            | `1`                                                |
| Median token-counting and prefix-hashing time of at least 8 requests in the last 2 seconds reaches a quarter of `slo.ttft_s` | `429` | `server_overloaded_error`                            | `1`                                                |
| Admission queue is full                                                                                                      | `429` | `server_overloaded_error`                            | `1`                                                |
| Admission wait expires before response headers                                                                               | `504` | `queue_expired`                                      |                                                    |
| Original request deadline expires before response headers                                                                    | `504` | `request_expired` or `expired`                       |                                                    |
| Projected TTFT exceeds the budget for a prompt that fits alone                                                               | `429` | `server_overloaded_error`                            | Budget overrun in seconds, rounded up, minimum `1` |
| Prompt alone exceeds the TTFT budget                                                                                         | `429` | `server_overloaded_error`                            |                                                    |
| Zero prefill engines are live and the decode target holds resident decode work                                               | `429` | `server_overloaded_error`                            | `1`                                                |
| Peak projected decode work over the request's decode window exceeds live decode capacity                                     | `429` | `server_overloaded_error`                            | `1`                                                |
| Decode load pushes the request past `slo.tpot_s` on every live decode engine                                                 | `429` | `server_overloaded_error`                            | `1`                                                |
| Zero engines are eligible for placement                                                                                      | `503` | `backend_unavailable` or `no_schedulable_engines`    | `1`                                                |
| Router is standby, fenced, in a whole-wave hold, awaiting engine identity validation, or in degraded engine monitoring       | `503` | `standby`                                            | `1`                                                |

Shorten the prompt or raise `slo.ttft_s` to clear a 429 for an oversized prompt.

`/ready` reports the reason for each `503` refusal.

[`serving.admission`](../configuration/02-Serving-and-Role-Control.md#41-global-admission) modes:

| Mode                   | Enforced                                                                                         |
| ---------------------- | ------------------------------------------------------------------------------------------------ |
| `predictive` (default) | Every `open` check and limit, plus the predictive TTFT, decode-capacity, and `slo.tpot_s` checks |
| `open`                 | Router saturation checks and the HTTP retention, queue, and phase-concurrency limits             |

### Admission counters

| Counter            | Increments on                                                      |
| ------------------ | ------------------------------------------------------------------ |
| `offered`          | Each request to a completion route                                 |
| `unsized_offered`  | Termination before workload sizing                                 |
| `rejected`         | HTTP `429` capacity refusal or HTTP `503` router-readiness refusal |
| `refused`          | Global predictive refusal                                          |
| `invalid_requests` | HTTP `400`, `404`, or `413` refusal before admission               |

## Streaming and response assembly

### Streaming responses

Streaming deltas use the engine's response shape under the [token-ID rules](#token-identity-and-output-accounting).

### Non-streaming assembly

Non-streaming responses assemble the engine stream as follows:

| Engine output            | Assembly behaviour                                                                               |
| ------------------------ | ------------------------------------------------------------------------------------------------ |
| Chat `content`           | String deltas concatenate under `content`                                                        |
| Chat `reasoning`         | String deltas concatenate under `reasoning`                                                      |
| Chat `reasoning_content` | String deltas concatenate under `reasoning_content`                                              |
| Chat `refusal`           | String deltas concatenate under `refusal`                                                        |
| `tool_calls`             | One call per stream index, returned in index order                                               |
| Tool call fields         | ID, function name, and arguments concatenate separately per call                                 |
| Tool call arguments      | Returned as engine-generated strings                                                             |
| Legacy `function_call`   | Function name and argument fragments concatenate into one message field                          |
| Chat logprobs            | `content` and `refusal` arrays concatenate in stream order                                       |
| Text-completion logprobs | `tokens`, `token_logprobs`, `top_logprobs`, and `text_offset` arrays concatenate in stream order |

Assembly returns HTTP `502` when:

- an unsupported choice or chat-delta field carries a value
- a supported field has a malformed value
- a tool call is missing its ID or function name

### Metadata and usage

Assembled responses carry the engine's response metadata and these derived fields:

| Field                             | Source                                                      |
| --------------------------------- | ----------------------------------------------------------- |
| `finish_reason`, `stop_reason`    | Last choice that reports each                               |
| `usage`                           | Non-null engine `usage`, including a later usage-only frame |
| `usage`, when the engine omits it | Computed from input length and measured output token count  |

## Token identity and output accounting

Decode requests to supporting engines include:

```json
{
  "return_token_ids": true,
  "stream_interval": 1
}
```

Every event with text, reasoning, tool calls, or refusals carries a token-ID list:

| Token-ID list                                | Result                                        |
| -------------------------------------------- | --------------------------------------------- |
| Nonnegative integers                         | Accepted                                      |
| Missing or malformed, including a Boolean ID | Decode attempt or profiling measurement fails |

`token_accounting` by engine:

| Engine             | Router reports                  |
| ------------------ | ------------------------------- |
| Supplies token IDs | `token_accounting: token_ids`   |
| Omits token IDs    | `token_accounting: unavailable` |

Clients that set `return_token_ids` receive the IDs in streaming and assembled responses.
