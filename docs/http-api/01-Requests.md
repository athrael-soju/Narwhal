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

Fields outside the router validation set pass through unchanged.

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

---

## Request identity and authentication

Narwhal assigns a router request ID at ingress, returns it as `x-request-id`, and derives a backend ID for each engine attempt and execution phase to track KV ownership. The request journal stores the forwarded client request ID as `client_rid` for correlation.

Ingress authenticates clients, strips client credentials and client-supplied internal IDs, then installs trusted values that Narwhal uses for client identity. Set `engine.engine_api_key_env` to attach the deployment's engine credential to serving and control requests.

See [Configure Narwhal](../configuration/03-Recovery-and-Validation.md#10-engine-authentication-and-protocol-adapters) for the engine authentication boundary and [Operate Narwhal](../operate/01-Start-Routers.md#3-configure-the-client-path) for ingress requirements.
