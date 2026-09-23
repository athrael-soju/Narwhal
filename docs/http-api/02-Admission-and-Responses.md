# Admission and responses

## Admission and refusal semantics

| Condition                                                                    |  HTTP | Result                                                           |
| ---------------------------------------------------------------------------- | ----: | ---------------------------------------------------------------- |
| Invalid JSON, body shape, or router-interpreted field type                   | `400` | `invalid_request_error`; affected field appears in `param`       |
| Requested model differs from the configured model                            | `404` | `model_not_found`                                                |
| `n > 1` or `best_of > 1`                                                     | `400` | Invalid sampling width                                           |
| Unsupported non-streaming audio, modality, or tool request                   | `400` | `invalid_request_error` naming the option in `param`             |
| Request exceeds `serving.max_request_bytes`                                  | `413` | `request_too_large`                                              |
| HTTP retention limit is full                                                 | `429` | `Retry-After: 1`                                                 |
| Admission queue is full                                                      | `429` | `Retry-After: 1`                                                 |
| Admission wait expires before response headers                               | `504` | Terminal expiry                                                  |
| Original request deadline expires before response headers                    | `504` | Terminal expiry                                                  |
| Predictive admission prices queued work above the TTFT budget                | `429` | `Retry-After` contains the rounded queue overrun                 |
| Prompt alone exceeds the TTFT budget                                         | `429` | Error envelope; shorten the prompt or raise the target           |
| Scheduler finds zero placement-eligible engines                              | `503` | `backend_unavailable`, `Retry-After: 1`                          |
| Router is standby, fenced, in whole-wave maintenance, or monitoring-degraded | `503` | Retryable refusal, `Retry-After: 1`; `/ready` reports the reason |

Set:

```text
admission: open
```

to disable predictive refusal.

Concurrency limits remain active.

### Admission counters

Narwhal records one `offered` arrival for each request to a completion route and increments `unsized_offered` when the body ends before workload sizing.

Global accounting classifies terminal conditions as follows:

- capacity rejection: `rejected`
- predictive refusal: `refused`
- malformed request body: `invalid_requests`

---

## Streaming and response assembly

### Streaming responses

Narwhal forwards streaming delta fields in the engine's response shape, applying its token-ID exposure rules.

### Non-streaming assembly

For non-streaming clients, Narwhal consumes the engine stream and assembles one final response.

| Engine output            | Assembly behaviour                                                                                                        |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------- |
| Chat `content`           | String deltas concatenate under `content`                                                                                 |
| Chat `reasoning`         | String deltas concatenate under `reasoning`                                                                               |
| Chat `reasoning_content` | String deltas concatenate under `reasoning_content`                                                                       |
| Chat `refusal`           | String deltas concatenate under `refusal`                                                                                 |
| `tool_calls`             | Calls are grouped by stream index and returned in index order; ID, function name, and arguments concatenate independently |
| Legacy `function_call`   | Function name and argument fragments concatenate into one message field                                                   |
| Chat logprobs            | Content and refusal arrays concatenate in stream order                                                                    |
| Text-completion logprobs | Token, logprob, and offset arrays concatenate in stream order                                                             |

Every tool call must contain:

- an ID
- a function name

Tool arguments remain engine-generated strings for the client to interpret.

The non-streaming assembler returns HTTP `502` for unsupported choice or chat-delta fields that carry a value, including audio, annotations, and custom tool output, and for malformed supported fields.

### Metadata and usage

Non-streaming assembly retains:

- response metadata
- non-null engine `usage`
- `finish_reason`
- optional `stop_reason`

If the engine omits `usage`, Narwhal computes it from:

- input length
- measured output token count

`finish_reason` and `stop_reason` survive later usage-only frames.

---

## Token identity and output accounting

For engines exposing token IDs, Narwhal adds the following to decode requests:

```json
{
  "return_token_ids": true,
  "stream_interval": 1
}
```

Narwhal counts identified output tokens across:

- text
- reasoning
- tool-call output

A valid token ID is a nonnegative integer.

Booleans are invalid token IDs.

Serving and profiling paths that require exact counting require valid token identity for:

- text
- reasoning
- tool-call output
- refusal output

Invalid identity fails the decode attempt or measurement.

The `token_ids` accounting dialect provides exact token identity for:

- output length
- TPOT scoring

Other accounting dialects report:

```text
unavailable
```

The following features require identified tokens:

- decode correction
- drift scoring
- output-length learning

Clients requesting `return_token_ids` receive token IDs in both streaming and non-streaming responses.
