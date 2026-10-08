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
| The projected TTFT plus the projected [decode slot wait](../configuration/02-Serving-and-Role-Control.md#decode-admission-check) exceeds the budget | `429` | `server_overloaded_error`                            | `1`                                                |
| The peak KV tokens projected in decode during the request's decode hold exceed live decode capacity                          | `429` | `server_overloaded_error`                            | `1`                                                |
| Decode load pushes the request past `slo.tpot_s` on every live decode engine                                                 | `429` | `server_overloaded_error`                            | `1`                                                |
| Zero live engines can take the request's prefill or decode leg                                                                                      | `503` | `backend_unavailable` or `no_schedulable_engines`    | `1`                                                |
| Router is standby, fenced, in a whole-wave hold, awaiting engine identity validation, or in degraded engine monitoring       | `503` | `standby`                                            | `1`                                                |

Shorten the prompt or raise `slo.ttft_s` to clear a 429 for an oversized prompt.

`/ready` reports the reason for each `503` refusal.

[`serving.admission`](../configuration/02-Serving-and-Role-Control.md#41-global-admission) selects the admission mode. `open` enforces the router saturation checks and the HTTP retention, queue, and phase-concurrency limits. `predictive`, the default, adds the predictive TTFT, decode-capacity, and `slo.tpot_s` checks to every `open` check and limit.

### Admission counters

The router keeps these admission counters:

| Counter            | Increments on                                                      |
| ------------------ | ------------------------------------------------------------------ |
| `offered`          | Each request to a completion route                                 |
| `unsized_offered`  | Termination before workload sizing                                 |
| `rejected`         | HTTP `429` capacity refusal or HTTP `503` router-readiness refusal |
| `refused`          | Global predictive refusal                                          |
| `invalid_requests` | HTTP `400`, `404`, or `413` refusal before admission               |

Journal rows and `/metrics` split `rejected` and `refused` by [outcome reason](../telemetry/01-Journal.md#outcome-reasons).

## Streaming and response assembly

### Streaming responses

Streaming deltas use the engine's response shape under the [token-ID rules](#token-identity-and-output-accounting).

The router relays the engine stream with these rules:

| Engine stream                         | Client stream                                                                                        |
| ------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| Line ends                             | CRLF, CR and LF each end an SSE line                                                                 |
| `data:` event                         | One client event in compact JSON, with token fields removed unless the client set `return_token_ids` |
| Malformed `data:` payload             | Relayed as received, counted as zero tokens                                                          |
| `data:` events before the first token | Held until the first token, up to 64 events and `serving.max_response_bytes`                         |
| Other lines before the first token    | Dropped                                                                                              |
| Other lines after the first token     | Relayed, each as its own event                                                                       |
| One transport read                    | One client write holding that read's events                                                          |

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

Assembled responses carry the engine's response metadata. `finish_reason` and `stop_reason` come from the last choice that reports each.

`usage` carries the non-null engine `usage`, including one from a later usage-only frame. When the engine omits `usage`, the router computes it from the input length and the measured output token count.

## Token identity and output accounting

Decode requests to supporting engines include:

```json
{
  "return_token_ids": true,
  "stream_interval": 1
}
```

Every event with text, reasoning, tool calls, or refusals carries a token-ID list of nonnegative integers. A missing or malformed list, including one with a Boolean ID, fails the decode attempt or profiling measurement.

The router reports `token_accounting: token_ids` for an engine that supplies token IDs. For an engine that omits them, it reports `token_accounting: unavailable`.

Clients that set `return_token_ids` receive the IDs in streaming and assembled responses.
