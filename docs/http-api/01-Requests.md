# Completion requests

## Completion API

Both completion routes serve the model configured for the fleet and return OpenAI-compatible response shapes.

### `POST /v1/completions`

For a non-streaming completion, Narwhal returns `object: "text_completion"` with generated text in `choices[0].text`.

### `POST /v1/chat/completions`

For a non-streaming chat completion, Narwhal returns `object: "chat.completion"` and an assistant message in `choices[0].message`. Its `content` is `null` when the response contains reasoning or tool-call output alone.

---

## Request contract

Narwhal parses each completion request as a JSON object and validates router-interpreted fields before reserving admission or engine capacity.

### Validated field types

Non-null values must use the following types:

| Field        | Required type                 |
| ------------ | ----------------------------- |
| `model`      | String                        |
| `stream`     | Boolean                       |
| `n`          | Integer; booleans are invalid |
| `best_of`    | Integer; booleans are invalid |
| `max_tokens` | Integer; booleans are invalid |
| `prompt`     | String or array               |
| `messages`   | Array of objects              |

Narwhal returns HTTP `400` in an OpenAI error envelope for invalid JSON, body shape, or router-interpreted field type. It also writes one [terminal request record](../telemetry/01-Journal.md#terminal-request-records) with `terminal: "invalid"`.

Example:

```json
{
  "error": {
    "message": "max_tokens must be an integer",
    "type": "invalid_request_error",
    "param": "max_tokens",
    "code": null
  }
}
```

For ordinary requests, fields outside the router validation set pass through
unchanged. If present, `narwhal_continuation` must be a Boolean, including
when continuation is disabled; `null` is invalid. Narwhal consumes this field
and removes it from engine requests.

### Model handling

Narwhal checks the requested `model` before dispatch and returns HTTP `404` with `model_not_found` if it names another model. For accepted requests, Narwhal sets the engine request's `model` to the configured model.

### Sampling width

Narwhal returns HTTP `400` for `n > 1` or `best_of > 1` because the one-token prefill leg and decode leg must use the same sampling width.

### Output and tool restrictions

Non-streaming requests accept text output and function tools. Narwhal returns HTTP `400` before engine dispatch for:

- `audio`
- an output `modalities` value other than `["text"]`
- a tool type other than `function`

The error uses `invalid_request_error` and identifies the rejected option in `param`.

Model and engine configuration determine actual support for input formats, reasoning, and function tools.

### Continuation opt-in

The unreleased continuation implementation can recover an eligible upstream
failure on the same router and client connection. It sends the exact original
prompt IDs and committed output IDs through a fresh prefill and decode
attempt. Live worker-failure qualification remains pending in
[#195](https://github.com/athrael-soju/Narwhal/issues/195).

Continuation requires both `continuation.enabled: true` in the fleet
configuration and `narwhal_continuation: true` in the request. An omitted or
`false` request field uses ordinary serving. An explicit opt-in while the
deployment policy is disabled returns HTTP `400`.

Opted-in requests must use `/v1/completions`, `stream: true`, one nonempty flat
array of nonnegative integer prompt IDs, and an explicit positive
`max_tokens`. IDs must fit the qualified vocabulary. The original prompt
length plus `max_tokens` must fit both the configured context bound and the
qualified backend context bound; the output limit must also fit the
qualified backend output bound.

Narwhal supplies the [qualified generation settings](../concepts/04-Stream-Continuation.md#request-boundary)
on every attempt. Explicit values must match those settings. Only these
additional fields are accepted:

| Field | Accepted values |
| --- | --- |
| `model` | Uses the [model handling](#model-handling) rules above |
| `stop` | `null`, `[]`, or omitted |
| `stop_token_ids` | An array containing only qualified stop IDs, or omitted |
| `return_token_ids` | Boolean, or omitted |
| `stream_interval` | `1`, or omitted |
| `stream_options` | `null`, omitted, or an object containing only optional Boolean `include_usage` and optional `continuous_usage_stats: false` |

Text prompts, chat, batches, nested token arrays, additional sampling
controls and unrecognised fields return HTTP `400` before engine I/O.
In particular, token stops require qualification; string stops are unsupported.

After committing output, Narwhal may recover transport failures, supported
transient engine errors or a stream that ends before a complete `[DONE]`
event. It discards uncommitted output and requests only the remaining token
allowance. The outward response ID, creation time, model and original usage
accounting remain stable across attempts. Failed or cancelled client writes
terminate the request.

Recovery requires a qualified survivor, an unexpired original deadline,
remaining output tokens and separate recovery attempts and credits. Invalid
token IDs or event framing, stale qualification, history-limit exhaustion
and local HTTP-pool starvation terminate the request. Recovery gate failures
use fixed SSE errors with `type: "continuation_error"` and a
`continuation_<reason>` code; see
[continuation failures](03-Backend-and-Failures.md#continuation-failures).

Before dispatch, the router reserves the full per-request history quota.
Insufficient space for the requested prompt and output returns HTTP `400`.
When other requests occupy the router's history ceiling, admission returns
HTTP `429` with `Retry-After: 1`. See the
[continuation configuration](../configuration/02-Serving-and-Role-Control.md#44-opt-in-continuation-state)
for byte limits and qualification inputs.

---

## Request identity and authentication

Narwhal assigns a router request ID at ingress, returns it as `x-request-id`, and derives a backend ID for each engine attempt and execution phase to track KV ownership. The request journal stores the forwarded client request ID as `client_rid` for correlation.

Ingress authenticates clients, strips client credentials and client-supplied internal IDs, then installs trusted values that Narwhal uses for client identity. Set `engine.engine_api_key_env` to attach the deployment's engine credential to serving and control requests.

See [Configure Narwhal](../configuration/03-Recovery-and-Validation.md#10-engine-authentication-and-protocol-adapters) for the engine authentication boundary and [Operate Narwhal](../operate/01-Start-Routers.md#3-configure-the-client-path) for ingress requirements.
